# Mesh Potato

[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-pytest-green.svg)](docs/manual.md#development)

A chat bot for [MeshCore](https://meshcore.co.uk) LoRa mesh channels. It runs on
a computer with a MeshCore companion radio on USB, answers questions with a
language model running on that same computer, and replies in one sentence that
fits a 160-byte radio packet. No cloud account, no web server, no database to
run. A live instance answers on `#ai` in southern Wisconsin, centered on Madison.

![The Mesh Potato terminal monitor with three channel columns](docs/tui.svg)

## What it does

- **Answers on the air.** Ask "What does spreading factor change?" or "Why is
  the sky blue?" and get one short sentence back as `@[you] answer`. It
  remembers your recent exchanges, so "explain that more simply" works.
- **Knows the mesh it lives on.** It reads its own frequency, bandwidth and
  power from the radio, carries a small sourced LoRa reference, and can explain
  how your message reached it from that packet's hop count, RSSI and SNR.
- **Looks things up.** Weather, scores, prices, news and other changing facts
  trigger a web lookup with a source domain, or an honest "could not verify".
  Daily weather comes as a compact one-line forecast.
- **Plays chess.** An optional `#chess` channel runs Stockfish with saved games
  per player, adjustable difficulty, hints, draw claims and move history.
- **Reports traffic.** An optional `#traffic` channel announces serious
  Wisconsin 511 incidents and closures, answers "current alerts", and serves
  Madison Beltline travel times. Routine congestion stays quiet.
- **Has personalities.** Nice by default; `/funny`, `/snarky`, `/marvin`,
  `/pirate`, `/haiku` and `/serious` on request, reverting after two hours.
  A silly fortune arrives a little after six every morning.
- **Treats the channel as hostile.** Every incoming line, the assembled
  context and every outgoing reply pass a sub-millisecond prompt injection
  detector before they can reach the model or the radio.
- **Is polite on the air.** It targets about 2 percent of channel time for its
  own transmissions, measures that from the radio's airtime counters, never
  replies closer than 15 seconds apart, and backs off when the channel is busy.
- **Watches itself.** A terminal monitor shows each channel, the shared radio,
  rate limits and every decision. Headless mode writes JSON lines for services.

Everything runs locally: a local model through Ollama or any OpenAI-compatible
endpoint, a SQLite file for memory, and a Python package with a test suite that
needs no radio, model or network.

## Quick start

```bash
git clone https://github.com/mhcoen/meshpotato.git meshpotato
cd meshpotato
uv venv --python 3.12
uv pip install -e '.[dev]'
ollama pull qwen3:30b-a3b-instruct-2507-q4_K_M
cp config.example.toml config.toml   # set port, channel_idx, bot_name
.venv/bin/meshpotato --config config.toml
```

You need a Mac or Linux computer that stays on (an old laptop is ideal, 32 GB of
RAM or more for the default model), a MeshCore companion radio on USB with a
node name equal to `bot_name`, Python 3.11 or newer, and
[Ollama](https://ollama.com). The radio must already have your regional preset
and the channel it will serve; see
[Prepare the radio](docs/manual.md#prepare-the-radio).

Then say something on the channel. `/help` lists commands. Press `q` to quit
the monitor, or run with `--headless` as a service.

## Going further

- [The manual](docs/manual.md): installation in detail, radio setup, every
  configuration key, how a message is handled, rate limits, security,
  troubleshooting and development.
- [#chess](docs/chess/README.md): setup, commands, difficulty levels and how
  games survive restarts and lost packets.
- [#traffic](bot/traffic/README.md): the 511 API key, what gets announced, and
  how to browse current alerts.
- [Configuration reference](docs/configuration.md) and the
  [example config](config.example.toml).

## License

MIT; see [LICENSE](LICENSE). Search and extraction code adapted from Episodic
retains its [Apache-2.0 license](bot/EPISODIC-LICENSE).

**Michael H. Coen**, W1MHC/WRYV459. mhcoen@gmail.com | mhcoen@alum.mit.edu
