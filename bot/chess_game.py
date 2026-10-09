"""Persistent per-name games. Every accepted turn is committed before transmission."""
import asyncio
import copy
import hashlib
import re
import time

import chess

from bot.chess_commands import parse_command, new_options
from bot.chess_engine import EngineError
from bot.storage import StateError
from bot.channel_info import welcome


def parse_move(board, text):
    text = text.strip().rstrip('.!?')
    if text.lower() in {'castle', 'castle kingside', 'castle king side'}:
        text = 'O-O'
    elif text.lower() in {'castle queenside', 'castle queen side'}:
        text = 'O-O-O'
    # Coordinate notation, including a spoken description of the piece.
    match = re.fullmatch(r'(?:(?:my |the )?(pawn|knight|bishop|rook|queen|king) )?(?:from )?'
                         r'([a-h][1-8])(?:\s*(?:to|-|x)\s*|\s*)([a-h][1-8])'
                         r'(?:\s*(?:=|promote to|promotion|to)?\s*(queen|rook|bishop|knight|[qrbn]))?', text, re.I)
    if match:
        piece, start, end, promotion = match.groups()
        source = board.piece_at(chess.parse_square(start.lower()))
        if piece and (source is None or chess.piece_name(source.piece_type) != piece.lower()):
            raise ValueError(f'There is no {piece.lower()} on {start.lower()}.')
        promotion = {'queen': 'q', 'rook': 'r', 'bishop': 'b', 'knight': 'n'}.get((promotion or '').lower(), (promotion or '').lower())
        candidate = chess.Move.from_uci(start.lower()+end.lower()+promotion)
        if not promotion and source and source.piece_type == chess.PAWN and chess.square_rank(candidate.to_square) in {0, 7}:
            raise ValueError('Choose a promotion piece: queen, rook, bishop or knight; for example e7e8q.')
        if candidate not in board.legal_moves:
            raise ValueError('That move is illegal in your position. Try board, hint, or a move such as e2e4.')
        return candidate
    if match := re.fullmatch(r'(?:(?:my|the) )?(pawn|knight|bishop|rook|queen|king) (?:to )?([a-h][1-8])', text, re.I):
        kind = chess.PIECE_NAMES.index(match[1].lower())
        candidates = [m for m in board.legal_moves if board.piece_type_at(m.from_square) == kind
                      and m.to_square == chess.parse_square(match[2].lower())]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise ValueError('More than one legal move fits. Include the starting square, such as g1f3.')
        raise ValueError('That move is illegal in your position. Try board or hint.')
    try:
        move = board.parse_san(text)
        if not move or move not in board.legal_moves:  # null moves are never playable
            raise ValueError()
        return move
    except chess.AmbiguousMoveError:
        raise ValueError('That move is ambiguous. Include the starting square, such as g1f3.') from None
    except ValueError:
        raise ValueError('I could not match a legal move. Try e4, Nf3, e2e4, or "knight from g1 to f3". Say help for commands.') from None


def board_for(game):
    try:
        if (not isinstance(game, dict) or game['color'] not in {'white', 'black'}
                or not isinstance(game['moves'], list) or len(game['moves']) > 10000
                or not isinstance(game['result'], str) or len(game['result']) > 80
                or not isinstance(game['last'], str) or len(game['last']) > 160):
            raise ValueError('invalid game fields')
        level, _ = new_options(game['level'])
        if level != game['level']:
            raise ValueError('invalid saved difficulty')
        board = chess.Board()  # User messages can never install arbitrary positions.
        for token in game['moves']:
            move = chess.Move.from_uci(token)
            if move not in board.legal_moves or board.is_game_over():
                raise ValueError('illegal saved move')
            board.push(move)
        return board
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise StateError('Saved chess game failed validation; it has been preserved for recovery.') from exc


def ending(board):
    outcome = board.outcome()
    if outcome is None:
        return ''
    if outcome.winner is not None:
        return f'Checkmate. {"White" if outcome.winner else "Black"} wins.'
    reason = {chess.Termination.STALEMATE: 'stalemate', chess.Termination.INSUFFICIENT_MATERIAL: 'insufficient material',
              chess.Termination.SEVENTYFIVE_MOVES: '75-move rule', chess.Termination.FIVEFOLD_REPETITION: 'fivefold repetition'}
    return 'Draw: '+reason.get(outcome.termination, 'game over')+'.'


def move_history(game, argument, available):
    """Replay saved moves into SAN, keeping full move pairs within each packet."""
    if argument and not re.fullmatch(r'[1-9][0-9]{0,3}', argument):
        return 'Say moves or moves 2 for another page. history works too.'
    page = int(argument or '1')
    board = chess.Board()
    turns = []
    for token in game['moves']:
        move = chess.Move.from_uci(token)
        san = board.san(move)
        if board.turn == chess.WHITE:
            turns.append(f'{board.fullmove_number}.{san}')
        else:
            turns[-1] += ' '+san
        board.push(move)
    if not turns:
        return 'No moves recorded yet.'
    text = ' '.join(turns)
    if len(text) <= available:
        return text if page == 1 else 'Only one page. Say moves.'
    pages = []
    offset = 0
    while offset < len(turns):
        number = len(pages)+1
        prefix = f'{number}: '
        body = ''
        start = offset
        while offset < len(turns):
            candidate = body+(' ' if body else '')+turns[offset]
            suffix = f' | moves {number+1}' if offset+1 < len(turns) else ''
            if len(prefix+candidate+suffix) > available:
                break
            body = candidate
            offset += 1
        if offset == start:
            return 'History needs more room. Try a shorter radio name.'
        suffix = f' | moves {number+1}' if offset < len(turns) else ''
        pages.append(prefix+body+suffix)
    return pages[page-1] if page <= len(pages) else f'Choose moves 1 through moves {len(pages)}.'


class ChessGames:
    def __init__(self, engine, *, max_games=1000, clock=time.time):
        self.engine, self.max_games, self.clock = engine, max_games, clock
        self.store = None
        self.lock = asyncio.Lock()

    async def prepare(self, store):
        if store is None:
            raise StateError('Chess requires a persistent state database.')
        self.store = store
        await self.engine.start()

    async def stop(self):
        await self.engine.stop()

    async def respond(self, sender, text, packet_timestamp, available):
        """None means duplicate: do not resend a possibly delivered move."""
        async with self.lock:
            state = self.store.load_chess(sender) or {'version': 1, 'game': None, 'pending': None, 'receipts': []}
            state = copy.deepcopy(state)
            if 'game' not in state or 'pending' not in state:
                raise StateError('Invalid saved chess session.')
            pending = state['pending']
            if pending is not None:
                try:
                    level, color = new_options(pending['level']+' '+pending['color'])
                    if (level != pending['level'] or color != pending['color']
                            or not isinstance(pending['at'], (int, float))):
                        raise ValueError()
                except (KeyError, TypeError, ValueError):
                    raise StateError('Invalid saved chess restart request.') from None
            receipts = state.get('receipts')
            if not isinstance(receipts, list) or len(receipts) > 128:
                raise StateError('Invalid chess receipt history.')
            wire = type(packet_timestamp) is int and packet_timestamp > 0
            digest = hashlib.sha256(text.strip().encode()).hexdigest()
            key = f'{packet_timestamp}:{digest}' if wire else digest
            now = self.clock()
            for receipt in receipts:
                if not isinstance(receipt, dict) or not isinstance(receipt.get('at'), (int, float)):
                    raise StateError('Invalid chess receipt.')
                if receipt.get('key') == key and (wire or receipt is receipts[-1] and 0 <= now-receipt['at'] < 30):
                    return None
            game = state.get('game')
            board = board_for(game) if game is not None else None
            command = parse_command(text)
            try:
                reply, changed = await self._execute(state, board, command, available)
            except (ValueError, EngineError) as exc:
                # No move or receipt committed after an engine/parse failure.
                reply = str(exc)
                return reply if len(reply) <= available else 'Could not complete that request. Your game is unchanged. Try help or status.'
            if len(reply) > available:
                return 'Not enough room in one reply. Try status, last, or a shorter sender name.'
            if changed or game is not None:
                state['receipts'] = (receipts + [{'key': key, 'at': now}])[-128:]
                self.store.save_chess(sender, state, self.max_games)
            return reply

    async def _execute(self, state, board, command, available):
        kind, argument = command.kind, command.argument
        game = state['game']
        def fit(*variants):
            return next((text for text in variants if len(text) <= available), variants[-1]), False
        if kind == 'about':
            return welcome('chess'), False
        if kind == 'help':
            return fit('new easy; new 1600 black. Play e4/e2e4. hint=advice; suggest=move. board, moves, status, draw, claim, resign. /! optional.',
                       'new easy white/black; play e4. hint, suggest, moves, board. Say help topics.')
        if kind == 'help topics':
            return ('Help: help play, help game, help levels, help board, help draw, help moves.', False)
        if kind == 'help play':
            return ('new easy black or new 1600 white. Play e4 or e2e4. hint=advice; suggest=move.', False)
        if kind == 'help game':
            return fit('board, status, moves, last move, resign. new then confirm new restarts; cancel keeps your game.',
                       'board, status, moves, last move, resign. new then confirm new restarts.')
        if kind == 'help history':
            return fit('moves or history shows saved moves: 1.e4 e5 2.Nf3 Nc6. Long games use pages: moves 2. No move is played.',
                       'moves or history: saved moves like 1.e4 e5. Next page: moves 2. No move played.')
        if kind == 'levels' or kind == 'difficulty' and not argument:
            low, high = self.engine.rating_range
            return fit(f'Levels: beginner, novice/easy, intermediate/medium, advanced/hard, expert/master, or {low}-{high}. Try difficulty easy.',
                       f'beginner, novice, intermediate, advanced, expert; {low}-{high}. difficulty easy')
        if kind == 'help board':
            return fit('Ranks 8-1, files a-h. Uppercase=White, lowercase=Black, .=empty. P pawn, N knight, B bishop, R rook, Q queen, K king.',
                       'Ranks 8 to 1, files a to h. White=PNBRQK; Black=pnbrqk. N=knight; .=empty.')
        if kind == 'help draw':
            return fit('draw offers a draw; claim checks repetition/50-move rules. Claim with an intended move: claim Ng8. Offers may be declined.',
                       'draw offers; claim checks repetition/50-move rules. Intended move: claim Ng8.')
        if kind == 'new':
            level, color = new_options(argument)
            self.engine.options(level)
            if game is not None and not game['result']:
                state['pending'] = {'level': level, 'color': color, 'at': self.clock()}
                return (f'Start a new {level} game as {color.title()}? Your current game will end. Say confirm new or cancel within 2 minutes.', True)
            return await self._new(state, level, color)
        if kind == 'confirm':
            pending = state.get('pending')
            if not pending or not 0 <= self.clock()-pending['at'] <= 120:
                state['pending'] = None
                return ('No pending restart. Say new beginner, or new 1600 black.', game is not None)
            return await self._new(state, pending['level'], pending['color'])
        if kind == 'cancel':
            state['pending'] = None
            return ('Restart cancelled. Your current game is unchanged.', game is not None)
        if game is None:
            return ('No game yet. Say new beginner, new easy black, or new 1600. Say help for commands.', False)
        if kind == 'history':
            return move_history(game, argument, available), False
        if kind == 'last':
            return (game['last'], False)
        if kind == 'status':
            return (f'You: {game["color"].title()}; difficulty: {game["level"]}. '+(game['result'] or f'Move {board.fullmove_number}: your turn.'), False)
        if kind == 'board':
            rows = [f'{rank+1}:'+''.join(board.piece_at(chess.square(file, rank)).symbol() if board.piece_at(chess.square(file, rank)) else '.'
                                      for file in range(8)) for rank in range(7, -1, -1)]
            return fit(' '.join(rows)+'; abcdefgh; '+('Game over.' if game['result'] else ('White' if board.turn else 'Black')+' to move.'),
                       ' '.join(rows), ' '.join(row.replace(':', '') for row in rows))
        if game['result']:
            return (game['result']+' Say new to play again.', False)
        human = game['color'] == 'white'
        if board.turn != human:
            raise StateError('Saved chess game is not on the player turn; use status and contact the operator.')
        if kind == 'difficulty':
            level, _ = new_options(argument)
            self.engine.options(level)
            game['level'] = level
            return (f'Difficulty is now {level}. Your position is unchanged.', True)
        if kind in {'hint', 'suggest'}:
            move, _ = await self.engine.advice(board)
            if kind == 'suggest':
                return (f'Consider {board.san(move)} ({move.uci()}). This is advice; your move has not been played.', False)
            piece = chess.piece_name(board.piece_type_at(move.from_square))
            if board.is_check():
                tip = 'Your king is in check; look for a legal escape.'
            elif board.is_capture(move):
                tip = f'Look for a useful capture with your {piece}.'
            elif board.is_castling(move):
                tip = 'Consider castling to reposition your king and rook.'
            elif move.promotion:
                tip = 'Your pawn has a legal promotion available.'
            else:
                tip = f'Look at improving your {piece} on {chess.square_name(move.from_square)}.'
            return (tip+' Say suggest for a specific move.', False)
        if kind == 'resign':
            game['result'] = 'You resigned. '+('Black' if human else 'White')+' wins.'
            game['last'] = game['result']
            state['pending'] = None
            return (game['result']+' Say new to play again.', True)
        if kind in {'draw', 'claim'}:
            claiming = board.copy()
            if argument:
                claiming.push(parse_move(board, argument))
            if claiming.is_repetition(3) or claiming.is_fifty_moves():
                game['result'] = 'Draw claimed: '+('threefold repetition.' if claiming.is_repetition(3) else '50-move rule.')
            elif kind == 'claim':
                return ('No draw claim is valid now. You can claim with an intended move, or say draw to offer one.', False)
            else:
                _, player_score = await self.engine.advice(board)
                if len(board.move_stack) < 20 or player_score < -50:
                    return ('Draw offer declined. Your turn; the position is unchanged.', False)
                game['result'] = 'Draw agreed.'
            game['last'] = game['result']
            state['pending'] = None
            return (game['result']+' Say new to play again.', True)
        move = parse_move(board, argument)
        san = board.san(move)
        board.push(move)
        game['moves'].append(move.uci())
        reply = f'You: {san}.'
        if not ending(board):
            response = await self.engine.move(board, game['level'])
            reply += f' Me: {board.san(response)} ({response.uci()}).'
            board.push(response)
            game['moves'].append(response.uci())
        game['result'] = ending(board)
        reply += ' '+(game['result'] or ('Check! Your move.' if board.is_check() else 'Your move.'))
        game['last'] = reply
        state['pending'] = None
        return reply, True

    async def _new(self, state, level, color):
        board = chess.Board()
        game = {'color': color, 'level': level, 'moves': [], 'result': '', 'last': ''}
        reply = f'New {level} game. You are {color.title()}.'
        if color == 'black':
            move = await self.engine.move(board, level)
            reply += f' Me: {board.san(move)} ({move.uci()}).'
            game['moves'].append(move.uci())
        reply += ' Your move.'
        game['last'] = reply
        state['game'], state['pending'] = game, None
        return reply, True
