"""One bounded async Stockfish process; callers supply full independent positions."""
import asyncio
import random

import chess
import chess.engine


class EngineError(RuntimeError):
    pass


class Stockfish:
    def __init__(self, path='stockfish', think_s=0.2, *, rng=None):
        self.path, self.think_s = path, think_s
        self.rng = rng or random.Random()
        self.transport = self.protocol = None
        self.lock = asyncio.Lock()
        self.rating_range = (0, 0)
        self.name = 'Stockfish'

    async def start(self):
        if self.protocol is not None:
            return
        try:
            async with asyncio.timeout(5):
                self.transport, self.protocol = await chess.engine.popen_uci([self.path])
                required = {'Skill Level', 'UCI_LimitStrength', 'UCI_Elo'}
                if not required.issubset(self.protocol.options):
                    raise EngineError('The configured engine must support Stockfish difficulty controls.')
                option = self.protocol.options['UCI_Elo']
                self.rating_range = (option.min, option.max)
                self.name = self.protocol.id.get('name', 'Stockfish')
                await self.protocol.configure({'Threads': 1, 'Hash': 32})
        except BaseException as exc:
            await self.stop()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise EngineError('Could not start Stockfish; check chess_engine_path and its executable permissions.') from exc

    async def stop(self):
        protocol, transport = self.protocol, self.transport
        self.protocol = self.transport = None
        if protocol is not None:
            try:
                await asyncio.wait_for(protocol.quit(), 1)
            except (Exception, asyncio.CancelledError):
                pass
        if transport is not None:
            transport.close()
        if protocol is not None:
            try:
                await asyncio.wait_for(asyncio.shield(protocol.returncode), 1)
            except (Exception, asyncio.CancelledError):
                pass

    def options(self, level):
        if level.isdigit():
            if not self.rating_range[0] <= int(level) <= self.rating_range[1]:
                raise ValueError(f'Numeric ratings supported by this engine: {self.rating_range[0]}-{self.rating_range[1]}. Or choose beginner.')
            return {'UCI_LimitStrength': True, 'UCI_Elo': int(level)}
        return {'UCI_LimitStrength': False, 'Skill Level': {'beginner': 0, 'novice': 0, 'intermediate': 5, 'advanced': 12, 'expert': 20}[level]}

    async def _call(self, board, level, analyse=False):
        async with self.lock:
            try:
                await self.start()
                options = self.options(level)
                async with asyncio.timeout(self.think_s + 3):
                    # New game token clears engine assumptions when switching players.
                    if analyse:
                        return await self.protocol.analyse(board, chess.engine.Limit(time=self.think_s),
                                                           game=object(), options=options)
                    return await self.protocol.play(board, chess.engine.Limit(time=self.think_s),
                                                    game=object(), options=options)
            except ValueError:
                raise
            except BaseException as exc:
                await self.stop()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise EngineError('The chess engine did not finish. Your position is unchanged; try again.') from exc

    async def move(self, board, level):
        result = await self._call(board, level)
        move = result.move
        if level == 'beginner' and self.rng.random() < 0.35:
            move = self.rng.choice(list(board.legal_moves))
        if move not in board.legal_moves:
            raise EngineError('The engine returned an invalid move. Your position is unchanged.')
        return move

    async def advice(self, board):
        info = await self._call(board, 'expert', analyse=True)
        pv = info.get('pv', [])
        if not pv or pv[0] not in board.legal_moves or 'score' not in info:
            raise EngineError('The engine could not analyse this position. Try again.')
        return pv[0], info['score'].pov(board.turn).score(mate_score=100000)
