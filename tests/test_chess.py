"""Chess rules, durable sessions, and fake-radio delivery (no live bot)."""
import asyncio
import io
import os

import pytest

chess = pytest.importorskip('chess')

from bot.chess_commands import LEVELS, new_options, parse_command
from bot.chess_engine import EngineError, Stockfish
from bot.channel_info import welcome
from bot.chess_game import ChessGames, board_for, ending, parse_move
from bot.config import ConfigError
from bot.jsonlog import EventLog
from bot.service import ChannelError, Decision
from bot.storage import StateError, StateStore
from tests.conftest import FakeBackend, FakeClock, Harness, make_config


class FakeEngine:
    rating_range = (1320, 3190)
    options = Stockfish.options

    def __init__(self, moves=()):
        self.moves = list(moves)
        self.calls = []
        self.score = 0
        self.error = None
        self.stopped = False

    async def start(self):
        pass

    async def stop(self):
        self.stopped = True

    async def move(self, board, level):
        self.calls.append((board.fen(), level))
        if self.error:
            raise self.error
        return board.parse_san(self.moves.pop(0)) if self.moves else next(iter(board.legal_moves))

    async def advice(self, board):
        self.calls.append((board.fen(), 'advice'))
        if self.error:
            raise self.error
        return next(iter(board.legal_moves)), self.score


@pytest.fixture
async def games(tmp_path):
    store = StateStore(str(tmp_path / 'games.sqlite3'), 'test-chess')
    engine = FakeEngine()
    clock = FakeClock(1000)
    games = ChessGames(engine, clock=clock)
    await games.prepare(store)
    yield games, engine, store, clock
    await games.stop()
    store.close()


async def ask(games, text, sender='Alice', timestamp=0, available=136):
    return await games.respond(sender, text, timestamp, available)


@pytest.mark.parametrize('level,alias', [(level, alias) for level, aliases in LEVELS.items() for alias in aliases])
def test_difficulty_synonyms(level, alias):
    assert new_options(alias+' as black') == (level, 'black')


@pytest.mark.parametrize('text,kind,arg', [
    ('/new 1600 black', 'new', '1600 black'), ('!new very easy', 'new', 'very easy'),
    ('I want to start a new game as black', 'new', 'as black'), ('play chess', 'new', ''),
    ('help', 'help', ''), ('!help', 'help', ''), ('/help commands', 'help', ''),
    ('help me choose a move', 'hint', ''), ("I'm stuck", 'hint', ''), ('what should I play?', 'suggest', ''),
    ('/draw', 'draw', ''), ('would you accept a draw?', 'draw', ''), ('/claim f6g8', 'claim', 'f6g8'),
    ('claim draw', 'claim', ''), ('claim a draw Ng8', 'claim', 'Ng8'),
    ('new game', 'new', ''), ('new game beginner', 'new', 'beginner'),
    ('I resign', 'resign', ''), ('/move Nf3', 'move', 'Nf3'),
])
def test_command_vocabulary(text, kind, arg):
    assert (parse_command(text).kind, parse_command(text).argument) == (kind, arg)


@pytest.mark.parametrize('text,uci', [('e4', 'e2e4'), ('e2e4', 'e2e4'), ('e2 to e4', 'e2e4'),
                                     ('Nf3', 'g1f3'), ('my knight from g1 to f3', 'g1f3'), ('knight to f3', 'g1f3')])
def test_legal_move_formats(text, uci):
    assert parse_move(chess.Board(), text).uci() == uci


@pytest.mark.parametrize('text', ['e5', 'e2e5', 'king from g1 to f3', '--', '0000', 'drop a queen on e4'])
def test_illegal_moves_and_null_moves_rejected(text):
    with pytest.raises(ValueError):
        parse_move(chess.Board(), text)


def test_special_moves_and_ambiguity():
    castle = chess.Board('r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1')
    assert parse_move(castle, 'castle queenside').uci() == 'e1c1'
    assert parse_move(castle, '0-0').uci() == 'e1g1'
    en_passant = chess.Board('4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1')
    assert en_passant.is_en_passant(parse_move(en_passant, 'exd6'))
    promotion = chess.Board('4k3/P7/8/8/8/8/8/4K3 w - - 0 1')
    with pytest.raises(ValueError, match='promotion'):
        parse_move(promotion, 'a7a8')
    assert parse_move(promotion, 'a7 to a8 promote to knight').promotion == chess.KNIGHT
    ambiguous = chess.Board('4k3/8/8/8/8/8/3N3N/4K3 w - - 0 1')
    with pytest.raises(ValueError, match='ambiguous|More than one'):
        parse_move(ambiguous, 'Nf3')
    with pytest.raises(ValueError, match='More than one'):
        parse_move(ambiguous, 'knight to f3')


async def test_separate_users_colors_and_difficulty(games):
    g, engine, store, _ = games
    assert 'beginner' in await ask(g, 'new learning')
    engine.moves = ['e4']
    assert 'Me: e4' in await ask(g, 'new expert black', 'Bob')
    engine.moves = ['e5']
    assert 'Me: e5' in await ask(g, 'e4')
    assert store.load_chess('Alice')['game']['moves'] == ['e2e4', 'e7e5']
    assert store.load_chess('Bob')['game']['moves'] == ['e2e4']
    assert store.load_chess('Alice')['game']['level'] == 'beginner'
    assert store.load_chess('Bob')['game']['level'] == 'expert'


async def test_engine_failure_and_illegal_move_leave_game_unchanged(games):
    g, engine, store, _ = games
    await ask(g, 'new easy')
    before = store.load_chess('Alice')
    assert 'legal move' in await ask(g, 'e5')
    assert store.load_chess('Alice') == before
    engine.error = EngineError('Engine temporarily unavailable; try again.')
    assert 'unavailable' in await ask(g, 'e4')
    assert store.load_chess('Alice') == before
    assert 'unavailable' in await ask(g, 'new easy black', 'Bob')
    assert store.load_chess('Bob') is None


async def test_restart_confirmation_cancel_and_expiration(games):
    g, engine, store, clock = games
    await ask(g, 'new')
    engine.moves = ['e5']
    await ask(g, 'e4')
    old = store.load_chess('Alice')['game']
    assert 'confirm new' in await ask(g, 'new expert black')
    assert store.load_chess('Alice')['game'] == old
    await ask(g, 'cancel')
    assert store.load_chess('Alice')['pending'] is None
    clock.advance(31)
    await ask(g, 'new expert black')
    clock.advance(121)
    assert 'No pending' in await ask(g, 'confirm new')
    assert store.load_chess('Alice')['game'] == old
    await ask(g, 'new expert black')
    engine.moves = ['d4']
    assert 'Me: d4' in await ask(g, 'confirm new')
    assert store.load_chess('Alice')['game']['moves'] == ['d2d4']


async def test_duplicate_packets_and_restarts_do_not_replay_turns(games):
    g, engine, store, _ = games
    await ask(g, '!new novice', timestamp=100)
    engine.moves = ['e5']
    await ask(g, 'e4', timestamp=101)
    before = store.load_chess('Alice')
    restored = ChessGames(engine)
    await restored.prepare(store)
    assert await ask(restored, 'e4', timestamp=101) is None
    assert await ask(restored, '!new novice', timestamp=100) is None
    assert store.load_chess('Alice') == before
    assert 'Me: e5' in await ask(restored, 'last move', timestamp=102)
    assert len(engine.calls) == 1


async def test_disk_restart_restores_full_history(games, tmp_path):
    g, engine, store, _ = games
    await ask(g, 'new')
    engine.moves = ['e5']
    await ask(g, 'e4')
    store.close()
    reopened = StateStore(str(tmp_path / 'games.sqlite3'), 'test-chess')
    try:
        restored = ChessGames(engine)
        await restored.prepare(reopened)
        assert 'Me: e5' in await ask(restored, 'last')
        engine.moves = ['Nc6']
        assert 'Me: Nc6' in await ask(restored, 'Nf3')
        assert board_for(reopened.load_chess('Alice')['game']).fullmove_number == 3
    finally:
        reopened.close()


async def test_hints_suggestions_board_and_help_never_play(games):
    g, _, store, _ = games
    await ask(g, 'new')
    for text in ['hint', 'suggest', 'board', 'help', 'help levels', 'help board', 'help draw', 'status']:
        answer = await ask(g, text, available=123)
        assert answer and 'Not enough room' not in answer
        assert len(answer) <= 123
        assert store.load_chess('Alice')['game']['moves'] == []
    assert '8:rnbqkbnr' in await ask(g, 'board', timestamp=999)


async def test_fools_mate_ends_game(games):
    g, engine, store, _ = games
    engine.moves = ['e5', 'Qh4#']
    await ask(g, 'new')
    await ask(g, 'f3')
    assert 'Checkmate. Black wins.' in await ask(g, 'g4')
    assert 'Checkmate' in await ask(g, 'e4')
    assert len(store.load_chess('Alice')['game']['moves']) == 4
    assert 'New' in await ask(g, 'new', timestamp=100)


async def test_human_checkmate_does_not_ask_engine_for_move(games):
    g, engine, _, _ = games
    engine.moves = ['e5', 'Nc6', 'Nf6']
    await ask(g, 'new')
    for move in ['e4', 'Qh5', 'Bc4']:
        await ask(g, move)
    assert 'Checkmate. White wins.' in await ask(g, 'Qxf7#')
    assert len(engine.calls) == 3


def game_from_moves(moves, color='white'):
    board = chess.Board()
    tokens = []
    for san in moves.split():
        move = board.parse_san(san)
        tokens.append(move.uci())
        board.push(move)
    return {'color': color, 'level': 'novice', 'moves': tokens, 'last': 'Your move.', 'result': ''}


async def test_current_and_intended_move_draw_claims(games):
    g, _, store, _ = games
    base = {'version': 1, 'pending': None, 'receipts': []}
    base['game'] = game_from_moves('Nf3 Nf6 Ng1 Ng8 Nf3 Nf6 Ng1 Ng8')
    store.save_chess('Alice', base, 100)
    assert 'threefold repetition' in await ask(g, 'claim draw')
    base['game'] = game_from_moves('Nf3 Nf6 Ng1 Ng8 Nf3 Nf6 Ng1', 'black')
    store.save_chess('Bob', base, 100)
    assert 'threefold repetition' in await ask(g, 'claim draw Ng8', 'Bob')
    assert len(store.load_chess('Bob')['game']['moves']) == 7


async def test_offer_draw_is_not_automatically_granted_and_resign_ends_game(games):
    g, _, store, _ = games
    await ask(g, 'new')
    assert 'declined' in await ask(g, 'offer a draw')
    assert 'No draw claim' in await ask(g, 'claim')
    assert 'You resigned' in await ask(g, 'I resign')
    assert store.load_chess('Alice')['game']['result']


@pytest.mark.parametrize('color,score,accepted', [('white', 0, True), ('black', -50, True), ('white', -51, False), ('black', -200, False)])
async def test_draw_offer_uses_players_score_after_ten_moves(games, color, score, accepted):
    g, engine, store, _ = games
    moves = 'e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7 Re1 b5 Bb3 d6 c3 O-O h3 Nb8 d4 Nbd7'
    if color == 'black':
        moves += ' a4'
    game = game_from_moves(moves, color)
    store.save_chess('Alice', {'version': 1, 'game': game, 'pending': None, 'receipts': []}, 100)
    engine.score = score
    reply = await ask(g, 'draw')
    assert ('Draw agreed' in reply) == accepted
    assert ('declined' in reply) != accepted
    assert store.load_chess('Alice')['game']['moves'] == game['moves']


async def test_fivefold_repetition_ends_game_automatically(games):
    g, engine, store, _ = games
    game = game_from_moves('Nf3 Nf6 Ng1 Ng8 ' * 3 + 'Nf3 Nf6')
    store.save_chess('Alice', {'version': 1, 'game': game, 'pending': None, 'receipts': []}, 100)
    engine.moves = ['Ng8']
    assert 'fivefold repetition' in await ask(g, 'Ng1')
    assert store.load_chess('Alice')['game']['result']


async def test_cancelled_search_never_commits_half_a_turn(games, monkeypatch):
    g, engine, store, _ = games
    await ask(g, 'new')
    before = store.load_chess('Alice')
    started = asyncio.Event()
    async def hanging_move(board, level):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(engine, 'move', hanging_move)
    task = asyncio.create_task(ask(g, 'e4', timestamp=12))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.load_chess('Alice') == before
    monkeypatch.undo()
    engine.moves = ['e5']
    assert 'Me: e5' in await ask(g, 'e4', timestamp=12)


async def test_concurrent_duplicate_requests_play_only_once(games):
    g, engine, store, _ = games
    await ask(g, 'new')
    engine.moves = ['e5']
    replies = await asyncio.gather(*(ask(g, 'e4', timestamp=99) for _ in range(6)))
    assert sum(reply is not None for reply in replies) == 1
    assert len(engine.calls) == 1
    assert len(store.load_chess('Alice')['game']['moves']) == 2


async def test_small_reply_budget_and_invalid_rating_cannot_change_game(games):
    g, _, store, _ = games
    await ask(g, 'new')
    before = store.load_chess('Alice')
    assert '1320-3190' in await ask(g, 'difficulty 500')
    assert 'Not enough room' in await ask(g, 'new expert black', available=80)
    assert store.load_chess('Alice') == before
    assert len(await ask(g, 'invalid move', available=80)) <= 80


async def test_engine_timeout_and_cancellation_close_owned_process():
    class Protocol:
        def __init__(self):
            self.returncode = asyncio.get_running_loop().create_future()
            self.started = asyncio.Event()
        async def play(self, *args, **kwargs):
            self.started.set()
            await asyncio.Event().wait()
        async def quit(self):
            self.returncode.set_result(0)
    class Transport:
        closed = False
        def close(self):
            self.closed = True
    for cancel in [False, True]:
        engine = Stockfish(think_s=0.05)
        protocol, transport = Protocol(), Transport()
        engine.protocol, engine.transport = protocol, transport
        task = asyncio.create_task(engine.move(chess.Board(), 'expert'))
        await asyncio.wait_for(protocol.started.wait(), 1)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else EngineError):
            await asyncio.wait_for(task, 5)
        assert engine.protocol is None and transport.closed and protocol.returncode.done()


async def test_missing_stockfish_is_a_clear_startup_error(tmp_path):
    engine = Stockfish(str(tmp_path/'missing-engine'))
    with pytest.raises(EngineError, match='chess_engine_path'):
        await engine.start()
    assert engine.protocol is None and engine.transport is None


@pytest.mark.parametrize('fen,reason', [
    ('7k/5Q2/6K1/8/8/8/8/8 b - - 0 1', 'stalemate'),
    ('7k/8/6K1/8/8/8/8/8 w - - 0 1', 'insufficient material'),
    ('7k/8/6K1/8/8/8/R7/8 w - - 150 76', '75-move'),
])
def test_automatic_draw_rules(fen, reason):
    assert reason in ending(chess.Board(fen))


async def test_state_corruption_capacity_and_competing_owner_fail_closed(games, tmp_path):
    g, _, store, _ = games
    g.max_games = 1
    await ask(g, 'new')
    with pytest.raises(StateError, match='capacity'):
        await ask(g, 'new', 'Bob')
    assert store.load_chess('Bob') is None
    damaged = store.load_chess('Alice')
    damaged['game']['moves'] = ['e2e5']
    store.save_chess('Alice', damaged, 1)
    with pytest.raises(StateError, match='validation'):
        await ask(g, 'status')
    assert store.load_chess('Alice') == damaged
    other = StateStore(str(tmp_path / 'games.sqlite3'), 'test-chess')
    try:
        other.save_chess('Alice', damaged, 1)
        with pytest.raises(StateError, match='another bot'):
            store.save_chess('Alice', damaged, 1)
    finally:
        other.close()


@pytest.mark.parametrize('kwargs', [{'chess_channel_idx': 256}, {'chess_channel_idx': -2},
    {'chess_think_s': 0}, {'chess_max_games': 0}, {'chess_channel_idx': 1, 'state_db': ''}])
def test_invalid_chess_configuration(kwargs):
    with pytest.raises(ConfigError):
        make_config(**kwargs)


async def test_fake_radio_failed_send_commits_once_and_recovers_with_last(tmp_path):
    from meshcore import EventType
    cfg = make_config(chess_channel_idx=1, state_db=str(tmp_path/'radio.sqlite3'),
                      announce_startup=False, global_burst=20, sender_burst=20)
    h = Harness(cfg, FakeBackend(), FakeClock(), channel_name='#chess')
    engine = FakeEngine(['e5'])
    h.service.chess = ChessGames(engine)
    try:
        await h.service.start()
        async def say(text, stamp):
            return await h.service.handle_payload({'channel_idx': 1, 'text': 'Alice: '+text, 'sender_timestamp': stamp})
        await say('new', 1)
        h.mc.commands.send_result_type = EventType.ERROR
        assert await say('e4', 2) == Decision.DROP_SEND_FAILED
        assert await say('e4', 2) == Decision.IGNORED_CHESS_DUPLICATE
        assert len(h.sent) == 2
        h.mc.commands.send_result_type = EventType.OK
        assert await say('last', 3) == Decision.ANSWERED_CHESS
        assert 'Me: e5' in h.sent[-1][1]
        assert not h.backend.calls and len(engine.calls) == 1
    finally:
        await h.service.stop()
    assert engine.stopped


async def test_cli_routes_chess_slot_and_no_chat_jobs_on_chess(tmp_path, monkeypatch):
    from bot.cli import build_service
    from tests.test_channels import ChannelRadio
    monkeypatch.setattr('bot.chess_engine.Stockfish', lambda *args: FakeEngine())
    monkeypatch.setattr('bot.cli.make_backend', lambda cfg: FakeBackend('Hello.'))
    radio = ChannelRadio()
    radio.commands.names[2] = '#chess'
    cfg = make_config(chess_channel_idx=2, state_db=str(tmp_path/'state.sqlite3'), announce_startup=False,
                      tips_enabled=False, fortune_enabled=False, adaptive_enabled=False,
                      global_burst=10, sender_burst=10)
    assert cfg.channel_indices == (1, 2)
    service = build_service(cfg, radio, EventLog(stream=io.StringIO()), references=())
    try:
        await service.start()
        await radio.deliver('Alice: !new beginner', 2)
        assert radio.commands.sent[-1][0] == 2 and 'New beginner' in radio.commands.sent[-1][1]
        assert service.services[2].chess and not service.services[1].chess
        assert service.services[2].tips is None and service.services[2].fortune is None
        assert not service.services[2].cfg.web_enabled
        assert service.services[1].reply_queue is service.services[2].reply_queue
    finally:
        await service.stop()


async def test_wrong_channel_name_stops_before_broadcasting(tmp_path):
    cfg = make_config(chess_channel_idx=1, state_db=str(tmp_path/'radio.sqlite3'))
    h = Harness(cfg, FakeBackend(), FakeClock(), channel_name='#ai')
    h.service.chess = ChessGames(FakeEngine())
    try:
        with pytest.raises(ChannelError, match='chess_channel_idx'):
            await h.service.start()
        assert not h.sent
    finally:
        await h.service.stop()


@pytest.mark.parametrize('sender', ['Alice', '\U0001f31fAndy0'])
async def test_command_replies_pass_real_output_gate_and_wire_limits(tmp_path, sender):
    cfg = make_config(chess_channel_idx=1, state_db=str(tmp_path/'radio.sqlite3'),
                      global_burst=32, sender_burst=32).for_channel(1)
    h = Harness(cfg, FakeBackend(), FakeClock(), channel_name='#chess')
    h.service.chess = ChessGames(FakeEngine(['e5', 'd4']))
    commands = ['new easy', 'e4', 'hint', 'suggest', 'help', 'help levels', 'help board', 'help draw',
                'board', 'status', 'last move', 'draw', 'claim', 'difficulty hard', 'e2e5',
                'new expert black', 'confirm new', 'resign']
    try:
        await h.service.start()
        for stamp, command in enumerate(commands, 1):
            decision = await h.service.handle_payload({'channel_idx': 1, 'text': f'{sender}: {command}',
                                                       'sender_timestamp': stamp})
            assert decision == Decision.ANSWERED_CHESS, (command, decision, h.records[-3:])
            assert len(f'{cfg.bot_name}: {h.sent[-1][1]}'.encode()) <= 160
        assert len(h.sent) == len(commands) and not h.backend.calls
    finally:
        await h.service.stop()


async def test_failed_save_never_reports_a_completed_move(tmp_path, monkeypatch):
    cfg = make_config(chess_channel_idx=1, state_db=str(tmp_path/'radio.sqlite3'),
                      global_burst=10, sender_burst=10).for_channel(1)
    h = Harness(cfg, FakeBackend(), FakeClock(), channel_name='#chess')
    h.service.chess = ChessGames(FakeEngine(['e5']))
    try:
        await h.service.start()
        await h.say('Alice: new')
        before = h.service._state_store.load_chess('Alice')
        def fail_save(*args):
            raise StateError('disk full')
        monkeypatch.setattr(h.service._state_store, 'save_chess', fail_save)
        assert await h.say('Alice: e4') == Decision.ANSWERED_CHESS
        assert 'could not safely save' in h.sent[-1][1] and 'Me: e5' not in h.sent[-1][1]
        assert h.service._state_store.load_chess('Alice') == before
    finally:
        await h.service.stop()


@pytest.mark.skipif(not os.environ.get('MESHPOTATO_TEST_STOCKFISH'), reason='set MESHPOTATO_TEST_STOCKFISH for real-engine tests')
async def test_real_stockfish_legal_moves_all_difficulties_and_shutdown():
    engine = Stockfish(os.environ['MESHPOTATO_TEST_STOCKFISH'], 0.05)
    try:
        await engine.start()
        protocol = engine.protocol
        for level in [*LEVELS, str(engine.rating_range[0]), str(engine.rating_range[1])]:
            board = chess.Board()
            for _ in range(4):
                move = await engine.move(board, level)
                assert move in board.legal_moves
                board.push(move)
        board = chess.Board()
        move, score = await engine.advice(board)
        assert move in board.legal_moves and isinstance(score, int)
        assert not board.move_stack
    finally:
        await engine.stop()
    assert protocol.returncode.done()


@pytest.mark.skipif(not os.environ.get('MESHPOTATO_TEST_STOCKFISH'), reason='set MESHPOTATO_TEST_STOCKFISH for real-engine tests')
async def test_real_engine_crash_preserves_game_and_recovers(tmp_path):
    engine = Stockfish(os.environ['MESHPOTATO_TEST_STOCKFISH'], 0.05)
    store = StateStore(str(tmp_path/'real.sqlite3'), 'chess-test')
    games = ChessGames(engine)
    try:
        await games.prepare(store)
        await ask(games, 'new intermediate')
        before = store.load_chess('Alice')
        killed = engine.protocol
        engine.transport.kill()  # Only the disposable child created by this test.
        await asyncio.wait_for(asyncio.shield(killed.returncode), 2)
        assert 'did not finish' in await ask(games, 'e4', timestamp=1)
        assert store.load_chess('Alice') == before
        answer = await ask(games, 'e4', timestamp=1)
        assert 'You: e4. Me:' in answer
        assert len(board_for(store.load_chess('Alice')['game']).move_stack) == 2
        assert engine.protocol is not killed
    finally:
        await games.stop()
        store.close()


@pytest.mark.parametrize('text,arg', [('moves',''), ('!history',''), ('/moves 2','2'),
    ('history 3','3'), ('show moves',''), ('move history','')])
def test_history_commands(text, arg):
    command = parse_command(text)
    assert (command.kind, command.argument) == ('history', arg)


def recorded_moves(sans):
    board = chess.Board()
    for san in sans.split():
        board.push_san(san)
    return {'moves': [move.uci() for move in board.move_stack]}


@pytest.mark.parametrize('sans,expected', [
    ('e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O', '1.e4 e5 2.Nf3 Nc6 3.Bb5 a6 4.Ba4 Nf6 5.O-O'),
    ('f3 e5 g4 Qh4#', '1.f3 e5 2.g4 Qh4#'),
    ('e4 a6 e5 d5 exd6', '1.e4 a6 2.e5 d5 3.exd6'),
    ('a4 h5 a5 h4 a6 h3 axb7 hxg2 bxa8=Q gxh1=Q',
     '1.a4 h5 2.a5 h4 3.a6 h3 4.axb7 hxg2 5.bxa8=Q gxh1=Q'),
])
def test_history_uses_standard_notation(sans, expected):
    from bot.chess_game import move_history
    assert move_history(recorded_moves(sans), '', 136) == expected


@pytest.mark.parametrize('room', [40, 72, 116, 136])
def test_history_pages_preserve_every_move_and_fit(room):
    from bot.chess_game import move_history
    game = recorded_moves('e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7 Re1 b5 Bb3 d6 c3 O-O h3 Nb8 d4 Nbd7')
    expected = move_history(game, '', 1000)
    parts = []
    for page in range(1, 30):
        reply = move_history(game, str(page), room)
        assert len(reply.encode()) <= room
        if reply == expected:
            parts.append(reply)
            break
        assert reply.startswith(f'{page}: ')
        body = reply.split(': ', 1)[1]
        parts.append(body.split(' | ')[0])
        if ' | ' not in reply:
            break
        assert reply.endswith(f' | moves {page+1}')
    assert ' '.join(parts) == expected
    assert 'Choose moves' in move_history(game, str(page+1), room) or 'Only one page' in move_history(game, str(page+1), room)


async def test_history_reads_saved_game_without_engine_or_position_changes(games, tmp_path):
    g, engine, store, _ = games
    assert 'No game yet' in await ask(g, 'moves')
    await ask(g, 'new easy white')
    assert await ask(g, 'history') == 'No moves recorded yet.'
    engine.moves = ['e5', 'Nc6']
    await ask(g, 'e4')
    await ask(g, 'Nf3')
    before = store.load_chess('Alice')['game']
    calls = len(engine.calls)
    assert await ask(g, 'moves') == '1.e4 e5 2.Nf3 Nc6'
    assert store.load_chess('Alice')['game'] == before
    assert len(engine.calls) == calls
    other = StateStore(str(tmp_path/'games.sqlite3'), 'test-chess')
    recovered = ChessGames(FakeEngine())
    try:
        await recovered.prepare(other)
        assert await ask(recovered, 'history') == '1.e4 e5 2.Nf3 Nc6'
        assert recovered.engine.calls == []
    finally:
        await recovered.stop()
        other.close()


@pytest.mark.parametrize('page', ['0', '-1', 'abc', '10000'])
def test_invalid_history_page_is_guidance(page):
    from bot.chess_game import move_history
    assert 'Say moves' in move_history(recorded_moves('e4 e5'), page, 136)


@pytest.mark.parametrize('notation,expected', [('0-0-0','e1c1'),('O-O-O','e1c1'),('0-0','e1g1'),('O-O','e1g1')])
def test_castling_zeros_and_letters(notation, expected):
    board = chess.Board('r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1')
    assert parse_move(board, notation).uci() == expected


@pytest.mark.parametrize('available', [80, 100, 112, 123, 136])
async def test_all_chess_help_topics_fit_small_reply_budgets(games, available):
    g, _, _, _ = games
    for command in ('help','help topics','help play','help game','help levels','help moves','help board','help draw'):
        answer = await ask(g, command, sender='A'*31, available=available)
        assert answer and len(answer) <= available and 'Not enough room' not in answer, (command, answer)
    await ask(g, 'new')
    answer = await ask(g, 'board', available=available)
    assert all(str(rank) in answer for rank in range(1,9))
    assert len(answer) <= available


async def test_long_name_can_get_help_on_radio(tmp_path):
    cfg = make_config(bot_name='Mesh Potato', reply_max_chars=147, chess_channel_idx=1, state_db=str(tmp_path/'long.sqlite3'), announce_startup=False)
    h = Harness(cfg, FakeBackend(), FakeClock(), channel_name='#chess')
    h.service.chess = ChessGames(FakeEngine())
    try:
        await h.service.prepare()
        assert await h.say('A'*31+': help') == Decision.ANSWERED_CHESS
        assert 'new easy' in h.sent[-1][1] and 'help topics' in h.sent[-1][1]
        assert len(('Mesh Potato: '+h.sent[-1][1]).encode()) <= 160
    finally:
        await h.service.stop()


async def test_newcomers_learn_games_are_personal_and_saved(games):
    g, engine, store, clock = games
    assert 'Everyone has their own saved game' in welcome('chess')
    assert 'Everyone has their own saved game' in await ask(g, 'status')
    assert 'own game is saved' in await ask(g, 'status', available=90)
    reply = await ask(g, 'new easy')
    assert reply.startswith('New novice game. You are White.')
    assert reply.endswith('Your game is saved; everyone has their own.')
    assert len(reply) <= 136
    short = await ask(g, 'new easy', sender='Bob', available=60)
    assert short == 'New novice game. You are White. Your move.'
    assert (await ask(g, 'last move', sender='Bob')) == 'New novice game. You are White. Your move.'


async def test_help_explains_personal_games_and_the_two_advice_commands(games):
    g, *_ = games
    full = await ask(g, 'help')
    assert full.startswith('Everyone has their own saved game.')
    assert 'hint (a piece), suggest (a move)' in full and 'hint=' not in full
    assert (await ask(g, 'help', available=112)).startswith('Own saved game per player.')
    assert (await ask(g, 'help', available=80)).startswith('Own game per player.')
    play = await ask(g, 'help play')
    assert 'suggest names a move without playing it' in play
