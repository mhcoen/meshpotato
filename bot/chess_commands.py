"""Small, deterministic vocabulary for a chess channel; no model decides moves."""
from dataclasses import dataclass
import re

LEVELS = {
    'beginner': ('beginner', 'new', 'learning', 'very easy'),
    'novice': ('novice', 'easy', 'casual'),
    'intermediate': ('intermediate', 'medium', 'normal'),
    'advanced': ('advanced', 'hard', 'strong'),
    'expert': ('expert', 'very hard', 'master'),
}


@dataclass(frozen=True)
class Command:
    kind: str
    argument: str = ''


def new_options(text: str) -> tuple[str, str]:
    text = text.lower().strip()
    color = 'black' if re.search(r'\bblack\b', text) else 'white'
    if re.search(r'\bblack\b', text) and re.search(r'\bwhite\b', text):
        raise ValueError('Choose White or Black, not both.')
    text = re.sub(r'\b(?:as|at|to|a|an|the|level|difficulty|rating|rated|elo|please|play|playing|with|white|black)\b', ' ', text)
    text = ' '.join(text.split())
    if not text:
        return 'novice', color
    if re.fullmatch(r'\d{3,4}', text):
        return text, color
    for level, aliases in LEVELS.items():
        if text in aliases:
            return level, color
    raise ValueError('Choose beginner, novice, intermediate, advanced, expert, or a numeric rating.')


def parse_command(text: str) -> Command:
    raw = text.strip().replace('’', "'")
    plain = re.sub(r'\s+', ' ', raw.lstrip('/!')).strip()
    q = plain.lower().rstrip('.?!')
    if q in {'about', 'version'}:
        return Command('about')
    if q in {'help', 'help commands', 'commands', 'how do i play', 'how do i start', 'how does this work', 'how do i use this'}:
        return Command('help')
    if q in {'help levels', 'help difficulty', 'levels'}:
        return Command('levels')
    if q in {'help board', 'help draw', 'help topics', 'help play', 'help game'}:
        return Command(q)
    if q in {'help history', 'help moves'}:
        return Command('help history')
    if m := re.fullmatch(r'(?:history|moves|move history|show moves|show history)(?: (.*))?', q):
        return Command('history', m[1] or '')
    if q in {'hint', 'help move', 'help moves', 'help with a move', 'help me choose a move',
             "i'm stuck", 'i am stuck', 'give me a hint', 'hint please', 'help me with a move', 'help me make a move', 'help with my move'}:
        return Command('hint')
    if q in {'suggest', 'suggest a move', 'what should i play', 'what is my best move', 'best move', 'what move should i make'}:
        return Command('suggest')
    if q in {'board', 'position', 'show my position', 'show the board', 'show my board'}:
        return Command('board')
    if q in {'status', 'whose turn', 'whose turn is it', 'my game'}:
        return Command('status')
    if q in {'last', 'last move', 'repeat', 'what was your move'}:
        return Command('last')
    if q in {'resign', 'i resign', 'i give up'}:
        return Command('resign')
    if q in {'draw', 'offer draw', 'offer a draw', 'would you accept a draw', "let's draw"}:
        return Command('draw')
    if m := re.fullmatch(r'(?:claim a draw|claim draw|claim)(?: (.+))?', plain.rstrip('.?!'), re.I):
        return Command('claim', m[1] or '')
    if q in {'yes', 'confirm', 'confirm new', 'yes start over', 'yes restart'}:
        return Command('confirm')
    if q in {'no', 'cancel', 'cancel restart', 'keep playing'}:
        return Command('cancel')
    if m := re.fullmatch(r'(?:new game|new|restart|start over|start(?: a)?(?: new)? game|'
                         r'i (?:want|would like) to start(?: a)?(?: new)? game|let\x27s play(?: chess)?)(?: (.*))?', q):
        return Command('new', m[1] or '')
    if q in {"i'm new to chess", 'i am new to chess'}:
        return Command('new', 'beginner')
    if q in {'play chess', 'start chess', 'i want to play chess', 'can we play chess'}:
        return Command('new')
    if m := re.fullmatch(r'(?:difficulty|set difficulty|change difficulty)(?: (.+))?', q):
        return Command('difficulty', m[1] or '')
    # Keep SAN case intact (b4 is a pawn; Bc4 is a bishop).
    move = re.sub(r'^(?:move|i play|i move)\s+', '', plain, flags=re.I).strip()
    return Command('move', move)
