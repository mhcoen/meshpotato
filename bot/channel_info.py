"""Channel identities have independent release versions, separate from the host."""

CHESS_NAME = 'Mesh Potato Chess'
CHESS_VERSION = '1.0'
TRAFFIC_NAME = 'Mesh Potato Traffic'
TRAFFIC_VERSION = '1.0'


def identity(kind):
    if kind == 'chess':
        return CHESS_NAME, CHESS_VERSION
    if kind == 'traffic':
        return TRAFFIC_NAME, TRAFFIC_VERSION
    raise ValueError('Unknown channel kind')


def welcome(kind, *, counties='Dane'):
    name, version = identity(kind)
    example = ('Say new beginner to play, e4 to move, hint for advice, or help.'
               if kind == 'chess' else
               f'Alerts for {counties}. Ask current alerts or traffic Beltline. Say help for more.')
    return f'{name} v{version}: {example}'
