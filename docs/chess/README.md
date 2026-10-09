# Mesh Potato Chess

Mesh Potato Chess runs saved games in **#chess** using Stockfish.

[Main README](../../README.md) · [Traffic README](../../bot/traffic/README.md)

## Setup

Chess is optional and disabled by default. Install the Python extra and a
[Stockfish executable](https://stockfishchess.org/download/):

```bash
uv pip install --python .venv/bin/python -e '.[chess]'
```

Run installation commands from the repository root. On macOS with Homebrew,
install the engine with `brew install stockfish`. Widget uses
`/opt/homebrew/bin/stockfish`; set the path appropriate for your computer.

Create a channel named **chess** or **#chess** on the companion radio and the
players' radios. Set its actual slot in `config.toml`, for example:

```toml
[chess]
chess_channel_idx = 3
chess_engine_path = "/absolute/path/to/stockfish"
chess_think_s = 0.2
chess_max_games = 1000
```

Slot 3 is only an example. Widget uses chess slot 3 and traffic slot 2. It is automatically added to the served slots; it
does not need to appear in `additional_channels`. Keep the AI channel in its
existing slot and run the same **single bot process**. Startup verifies the
channel name, saved-state ownership and engine before enabling any replies.
Mesh Potato Chess has its own version, initially **1.0**, independent of
Mesh Potato Traffic and the host program. `about` or `version` shows it.
Chess does not call Ollama or web search and sends no usage tips or fortunes. It shares the normal radio queue and rate limits.

## Playing

Each sender name has one saved game. Plain language, `/` and `!` prefixes work.

To begin, send `new beginner white` in **#chess**, wait for the new game reply,
then send `e4`. Choose `new beginner black` if you want the bot to play White
and make the first move. You can replace `beginner` with another difficulty or
a supported rating, such as `new 1600 black`. If you omit a color, you play White;
if you omit a difficulty, the game uses novice. Keep using the same radio name
to return to your saved game.

| Message | Action |
|---|---|
| `new beginner`, `!new easy black` | Start a game; White and novice are the defaults |
| `I want to start a new game as black` | Start with natural language |
| `/new 1600 white` | Select an approximate engine Elo |
| `difficulty hard`, `difficulty 1800` | Change strength without changing the position |
| `e4`, `Nf3`, `e2e4`, `knight from g1 to f3` | Play a legal move; the bot replies with its move |
| `castle kingside`, `e7e8q` | Castle or promote; promotions require a piece |
| `help`, `help topics`, `help play`, `help game`, `help levels`, `help board`, `help draw`, `help moves` | Instructions; no move is played |
| `hint`, `help me choose a move` | A positional hint |
| `suggest`, `what should I play?` | A specific legal move suggestion; does not play it |
| `board`, `status`, `last move` | See the position, game status or last turn's reply |
| `moves`, `history`, `!moves 2` | Saved moves in compact algebraic notation; request a numbered page for longer games |
| `draw`, `offer a draw` | Offer a draw, or claim an already valid draw |
| `claim`, `claim Ng8` | Claim threefold repetition or the 50-move rule, optionally with an intended move |
| `resign`, `I resign` | End the game by resignation |
| `new`, then `confirm new` | Replace an unfinished game; confirmation expires after two minutes |
| `cancel` | Keep the current game and cancel a pending restart |

Help automatically uses shorter replies for long radio names. `help topics`
lists the detailed pages. Compact boards preserve all eight ranks but may omit
the file legend and turn label; use `help board` for notation and `status` for
the turn. Castling accepts both `O-O`/`O-O-O` and `0-0`/`0-0-0`.

## Difficulty

Difficulty synonyms: **beginner** (new, learning, very easy), **novice** (easy,
casual), **intermediate** (medium, normal), **advanced** (hard, strong), and
**expert** (very hard, master). These use Stockfish skill settings 0, 0, 5, 12
and 20 respectively. Beginner additionally chooses a random legal move 35% of
the time. These levels are approximate, not calibrated human ratings. Numeric
ratings use the installed engine's advertised range (1320–3190 with the tested
Stockfish 19); `help levels` displays the actual range. Requests outside that
range are rejected without changing the game. The engine uses one thread,
32 MiB hash and a 0.2-second search by default; radio waits still apply.

## Board and game rules

The compact board lists ranks 8 through 1, each with files a through h.
Uppercase pieces are White, lowercase are Black, and `.` means empty. `N` is a
knight. Illegal or ambiguous moves leave the position unchanged. Checkmate,
stalemate, insufficient material, fivefold repetition and the 75-move rule end
the game automatically. Threefold repetition and the 50-move rule require a
claim. For an ordinary draw offer, the bot accepts after at least ten full moves
when its engine evaluation gives it no advantage greater than half a pawn;
otherwise it declines and leaves the player's turn unchanged.

## Saved games

Games and full move histories survive restarts in the channel's SQLite file.
Names identify games, so changing names starts a separate session, and two
people with the same name share a game. Names are not authenticated and all
replies are visible on the channel. The default limit is 1000 saved players;
existing games are never evicted to admit a new player.

## Recorded moves

`moves` or `history` shows the current game's recorded moves, for example
`1.e4 e5 2.Nf3 Nc6 3.Bb5 a6`. Numbers identify full moves, with White first and
Black second. Standard notation includes captures (`exd5`), castling (`O-O`),
promotion (`e8=Q`), check (`+`) and checkmate (`#`). A long history is split into
pages: a reply ending in `moves 2` tells you how to request the next page.
`history 2` works too. Only one page is sent per request; these commands never
play a move or call the engine. They also work after the game ends and after a
restart. Starting a new game replaces the history shown by these commands.

## If a chess reply does not arrive

A missing reply does not tell you whether your move arrived. The bot may still
be waiting for airtime, or it may have saved your move and its response while
the return message was lost. All three channels share the radio, so allow time
for a reply before sending another request.

1. Send `last move` to retrieve the last saved turn without playing another move.
   For example, after you send `e4`, a reply such as
   `You: e4. Me: e5 (e7e5). Your move.` confirms that both moves were saved.
2. If that reply still shows the previous turn, send `board` to check the saved
   position. `status` also shows your color, difficulty, move number and whether
   the game has ended.
3. If the saved position shows that your move was not accepted, resend it once.
   If it was accepted, continue from the bot's saved reply instead of replaying
   your move. If no requests receive replies, wait for the connection or bot to
   recover, then ask `last move` again.

Do not start a new game just to recover a missing reply. Games survive bot
restarts, and `last move`, `board` and `status` leave the position unchanged.
Automatic duplicate packet protection does not make every manually resent move
safe: a fresh message can be treated as a new request.

## Delivery and recovery details

Each accepted turn and its packet receipt are saved together **before sending**.
A repeated timestamped packet among the last 128 processed requests cannot play
twice. Without a packet timestamp, only consecutive identical requests within
30 seconds are suppressed. A radio-send failure may mean the reply was delivered,
so it is not automatically resent or replayed: ask `last move` or `status` to
recover. An engine failure leaves the saved position unchanged and the next
request can restart the engine. Back up the channel's state file with the others.

## First launch and restarts

The channel introduces itself as Mesh Potato Chess v1.0 on its first launch.
With the default `announce_once = true`, the saved welcome attempt keeps later
restarts quiet. Leave off `--no-announce` when starting it for the first time if
you want that introduction. `announce_startup = false` suppresses welcomes on
all channels. Welcomes and moves share the normal radio queue and airtime limits.

For installation, starting the shared process and terminal monitor controls,
see the [main README](../../README.md#usage).
