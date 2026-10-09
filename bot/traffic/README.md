# #traffic: Mesh Potato Traffic

Mesh Potato serves travel time questions and important Wisconsin 511 reports in
**#traffic**. It uses the 511 feeds directly. A language model does not decide
whether an incident happened, invent details, or rewrite an alert.

Automatic announcements cover serious incidents, significant road closures and
dangerous road conditions. Routine congestion and ordinary lane restrictions
do not generate broadcasts. Travel times are available when someone asks.

[#ai and main README](../../README.md) · [#chess README](../../docs/chess/README.md)

## Setup

Create **#traffic** on the companion radio and on the radios that will use it.
Set its actual slot in `config.toml`; this example uses slot 2, as configured on Widget:

```toml
[traffic]
traffic_channel_idx = 2
traffic_counties = "Dane"
traffic_lookahead_h = 24.0
traffic_announce_existing = true
traffic_enabled = true
traffic_refresh_s = 300.0
```

The slot is automatically served and does not also need to be listed in
`additional_channels`. Use the public channel name `#traffic`. It must be a
different slot and channel key from #ai and #chess. Leaving `traffic_channel_idx`
at its default of `-1` disables #traffic and preserves the
existing traffic answers in #ai.

The API key is read only from `WI511_API_KEY` in the process environment. The
legacy environment name `511_API_KEY` is accepted too, but normal shell
assignments cannot begin with a digit. Never put the key in `config.toml`, this
directory, or a command line argument. A private shell file can contain:

```zsh
export WI511_API_KEY='your key'
```

Store that file as `~/.secrets/511` with mode 600. Load it before starting the bot:

```zsh
source ~/.secrets/511
.venv/bin/meshpotato --config config.toml
```

For interactive shells, `.zshrc` can load it with:

```zsh
if [[ -r "$HOME/.secrets/511" ]]; then
    source "$HOME/.secrets/511"
fi
```

An already running bot or tmux session does not acquire new environment variables
automatically. Source the file in the shell that launches the bot. Services that
do not run an interactive shell need their own environment setup.

Run one bot process for #ai, #chess and #traffic. Channel setup, the API key's
presence and saved state are checked before channel handlers start. An API
outage is retried at the next poll. Help remains available; automatic alerts
require a successful feed snapshot no older than ten minutes. No radio channel
is created or renamed by this code.

## Asking for reports

Plain text, slash commands and exclamation commands all work. Each request sends
at most one reply so a list cannot flood the mesh.

| Send | Result |
| --- | --- |
| `What are the current alerts?`, `current alerts`, `/alerts`, `!alerts` | First significant current or upcoming report, with a count |
| `next`, `next alert`, `!next` | Next report in your list |
| `alerts 2` | A specific report |
| `traffic Beltline`, `What's traffic like on the Beltline?` | Cached travel times in both directions |
| `traffic I90 northbound` | Travel time between the Beltline and I94 |
| `help`, `/help`, `!help` | Commands and examples |
| `help policy` | What gets announced automatically |
| `help coverage` | Geographic scope and travel time coverage |
| `status` | Last successful feed check and number of significant reports |
| `about`, `version` | Name, independent version and coverage; includes this README link only when it fits |

For example, using the real schema of the October 10, 2026 WIS 19 closure:

```text
1 of 2. WIS 19 eastbound closed 10/10 6AM to 8PM: WIS 113 to River Rd. Say next.
```

This is an example, not a permanent announcement. Dates and times come from the
feed and use America/Chicago time. Upcoming closures say “closed” with the date
and time. Source labels are omitted from reports to save space. Reports fit the radio packet using shorter
complete wording, never cut off mid sentence. Very long reports fall back to
the road, direction, county and incident type.

Each sender has their own list position. A changed report list starts navigation
again at the first report. No matching significant reports does not mean every
road is clear. If the feed is stale, the bot explains that it cannot verify the
current alerts. It does not present an old alert list as current.

When this channel is enabled, road traffic questions in #ai direct
people to #traffic. Travel time answers and automatic traffic announcements
remain in #traffic. Questions about radio or network traffic still belong in #ai.
Other roads do not have a general live travel time lookup; the bot gives a
coverage explanation or asks for a street rather than inventing a report.

## Announcement rules

On the first successful startup, important reports that are still relevant can
be announced even if their 511 update is several days old. An upcoming full
closure like the WIS 19 example qualifies. By default, scheduled closures are
considered up to 24 hours ahead. Setting `traffic_announce_existing = false`
records the initial reports silently; subsequent new reports or changes can
still be announced.

The current rules select:

1. Incidents with a full closure, at least two blocked lanes, a major severity
   rating, or explicit serious details such as a pileup, rollover, vehicle fire,
   jackknifed vehicle, flooding, black ice or hazardous materials.
2. Full construction closures of through lanes on Interstate, US and Wisconsin
   highways. Ordinary construction, shoulder restrictions, ramp closures and
   construction on county roads do not produce automatic posts.
3. Winter road reports of ice, impassable or closed roads, or travel not advised.
   The source observation must be no more than one hour old. Duplicate driving
   and passing lane reports for the same stretch are grouped.
4. High importance 511 alerts explicitly covering a configured county or the
   whole state. Southwest region alerts also apply when Dane is configured.

The defaults cover Dane County. `traffic_counties` accepts a comma separated
list of county names. It does not expand the Beltline and I90 travel time
coverage. The rules are deliberately based on explicit feed data. A source
without enough detail can be omitted; the bot cannot guarantee comprehensive
incident coverage.

Saved receipts compare derived facts: road, direction, county, incident kind,
location, full closure status, blocked lane count, recognized hazard categories
and the scheduled window. Source descriptions remain available for rendering,
but cosmetic wording edits and changed polling timestamps do not trigger a
repeat. Common hazard spellings and "near" versus "at" are normalized.

Changed facts normally wait until 30 minutes after the last attempt for that
identifier. A changed full closure status, changed hazard set or increased
blocked lane count bypasses that interval. Other identifiers are unaffected.
The latest eligible facts are used when the interval expires; intermediate
edits are not queued. An unchanged report never repeats merely because time
has elapsed. The same rules apply after a restart.

Old receipts migrate to fingerprint version 2 silently on the first successful
poll containing that identifier. Their claim times are preserved. Previously
unseen identifiers remain eligible, and old receipts for absent reports stay
available for migration if those reports return. A failed migration write
preserves the old state and prevents an upgrade from causing repeat broadcasts.
This one-time baseline cannot distinguish a real change during the upgrade from
a change in fingerprint format, so it intentionally makes no repeat announcement.
A removed event does not produce an automatic “all clear,” because disappearance
alone is not proof the road reopened.

The three authenticated feeds are checked every five minutes by default. They
use three calls per poll, within the documented limit of ten calls per minute.
Polling must complete successfully before a new snapshot replaces the previous
one. Failed polls do not make old observations look fresh. Announcements stop
once the last successful snapshot is ten minutes old, and each report's end
time is checked again before transmission.

The existing travel time cache separately checks 511's public tables every five
minutes. It preserves original observation times and marks older reports with
their source times. Ordinary travel time changes never cause announcements.

All channels share the same radio allowance and congestion controls. Traffic
announcements yield to user requests and attempt at most one post per five
second scheduler tick. The radio limiter can impose a longer wait. The first
launch welcome is separate from alert broadcasts. Personality changes, usage tips and fortunes from #ai are not
posted to #traffic.

## Storage and failures

Announcement receipts are in the #traffic channel's SQLite database, for example
`meshpotato.channel-2.sqlite3`. Back it up with the other channel databases.
Changing a slot's channel identity requires a separate database. A persistent
database is required; disabling persistence would repeat alerts on every restart.

A receipt is saved immediately before the radio attempt, after validation and
rate admission. An ambiguous failed send, interrupted send or restart never
automatically retries that report. It might already have reached the mesh.
People can still request it with `alerts`. Busy channels, expired reports and
failed database writes do not mark an unsent report as delivered.

The history is bounded to 10,000 distinct report identifiers. It is not silently
evicted to make room for new reports. Corrupt or full history stops announcements
and logs an error; existing receipts are preserved for operator recovery.

Mesh Potato Traffic starts at version **1.0**, independent of the services in #chess and #ai. Say `about` or `version` to see it. Its first launch welcome explains
alerts and travel times; a saved receipt keeps routine restarts quiet.

The `--no-announce` switch suppresses all channel welcomes. It does not
disable significant traffic alerts. Their startup behavior is controlled by
`traffic_announce_existing`, and persistent receipts prevent restart repetition.

## Code and tests

| File | Purpose |
| --- | --- |
| `__init__.py` | Existing travel time cache and traffic question routing |
| `api.py` | Authenticated, bounded requests to the official 511 API |
| `alerts.py` | Report selection, stable change detection and plain wording |
| `channel.py` | Polling, saved announcement receipts, help and report navigation |

Run isolated tests from the repository root:

```bash
.venv/bin/pytest -q tests/test_traffic.py tests/test_traffic_channel.py
```

Tests use fake API responses, clocks and radios. They do not start a live bot,
access the actual key, or transmit. They cover first startup, restarts, changed
reports, stale data, API errors, failed or cancelled sends, help, routing and
radio packet limits.

API references: [511 developer documentation](https://511wi.gov/developers/doc),
[events](https://511wi.gov/help/endpoint/event),
[alerts](https://511wi.gov/help/endpoint/alerts), and
[winter road conditions](https://511wi.gov/help/endpoint/winterroads).

## Rejected reports

If the output safety gate rejects one report, other eligible reports still get
a chance to broadcast. The rejected event and content fingerprint are remembered
in memory and logged once, rather than retried on every tick. This rejection
key includes the wording so a correction can receive a fresh safety check; it is
separate from the derived facts used for announcement history. Changed content or
a process restart allows another check. Reports that cannot fit a radio packet
receive the same treatment. These rejections never create a sent receipt.
A failure after a radio attempt keeps its durable receipt and is not replayed.
No tick attempts more than one radio transmission.

A congestion subtype cannot qualify solely through a high severity label. A
reported full closure, multiple blocked lanes or a serious hazard can still
qualify. Description text contributes recognized hazard categories to the
fingerprint, rather than its original wording. The safety gate still checks the
actual rendered text before transmission.
