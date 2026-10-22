# #traffic: Mesh Potato Traffic

Mesh Potato serves travel time questions, important Wisconsin 511 reports and
official Madison transportation notices in **#traffic**. A language model does
not decide whether an incident happened, invent details, or rewrite an alert.

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
traffic_madison_enabled = true
traffic_madison_preview = false
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
require a successful check of their source within ten minutes. The 511 and city
sources refresh independently. No radio channel is created or renamed by this code.

## Asking for reports

Commands start with `/` or `!`; for example, `/reports` and `!reports` do the
same thing. `/reports` and `/cont` send at most five messages per request, two seconds apart
after the first message, sharing one normal request allowance. Other commands send one reply.

| Send | Result |
| --- | --- |
| `/reports` | Send the first five current or upcoming reports automatically, numbered; includes minor city restrictions |
| `/cont` | Send the next group of up to five reports |
| `/details` | Source link for the report just read |
| `/Beltline` | Cached travel times in both directions |
| `/I90 northbound` | Travel time between the Beltline and I94 |
| `/help` | How to read reports and where to find examples |
| `/examples` | Travel-time requests you can copy |
| `/commands` | Other commands, with an explanation of each |
| `/policy` | What gets announced automatically |
| `/coverage` | Geographic scope and travel time coverage |
| `/status` | Independent 511/city coverage and report count; with city notices disabled, last successful 511 check |
| `/about`, `/version` | Name, independent version, data sources and computer/radio arrangement; includes this README link when it fits |
| `/source` | GitHub repository link |
| `/readme` | This channel's README link |

`/help` explains the task rather than listing unexplained command names:

> /reports sends up to 5 numbered closure and incident reports. /examples shows travel-time requests. /commands explains other commands.

`/examples` gives working travel-time requests:

> /Beltline gives travel times both ways. Try /Beltline eastbound or /I90 northbound for one direction.

`/commands` explains the remaining commands, including `/coverage` (roads covered)
and `/policy` (automatic posts). Each help response fits one radio message.

For example, using the real schema of the October 10, 2026 WIS 19 closure:

```text
1 of 2. WIS 19 eastbound closed 10/10 6AM to 8PM: WIS 113 to River Rd.
```

This is an example, not a permanent announcement. Dates and times come from the
feed and use America/Chicago time. Upcoming 511 closures say “closed” with the date
and time; city notices say “planned”. Source labels are omitted to save space.
Reports fit the radio packet using shorter complete wording, never cut off mid sentence. Very long 511 reports fall back to
the road, direction, county and incident type. City reports that cannot fit direct
the reader to `/details`; their automatic announcements are withheld.

`/reports` sends the first five reports automatically, one numbered message per
report. There is no need to reply between messages. If there are more than five,
the fifth message ends with "Next 5: /cont"; `/cont` sends the next group.
The final report says "End of reports" when space allows. If that label would
prevent the complete report from fitting, it is omitted; "N of N" still marks the
end. `/reports` starts again. `/details`
gives the source for the last report viewed. A report too long to fit still
gets a numbered message pointing to `/details`.

The first message waits for normal admission and the initial reply delay.
Follow-up messages in that batch wait two seconds after the preceding send
finishes, without repeating the initial delay. Every message passes the output
checks. The entire batch uses one global and per-sender allowance, which refills
from the first send; follow-ups do not add a cooldown. At four requests per minute
and a one-request burst allowance, five reports take about eight seconds after
the first send, and another request becomes eligible about 15 seconds after that
first send (roughly seven seconds after the batch). Congestion can extend these
times: a radio pause stops sending and slows or stops allowance recovery.
Automatic announcements retain normal admission. A batch keeps the radio queue
until it finishes and is bounded by the configured request deadline. A send failure stops the batch without retrying that message. A changed
or stale source list also stops the batch; `/reports` starts with fresh data.

Each sender has their own list position. A changed report list starts navigation
again at the first report. No matching reports does not mean every road is clear.
Stale sources are excluded from the list; reports from a fresh source remain
available. Use `/status` to check each source. If neither source is fresh, the bot
explains that it cannot verify current alerts.

When this channel is enabled, road traffic questions in #ai direct
people to #traffic. Travel time answers and automatic traffic announcements
remain in #traffic. Questions about radio or network traffic still belong in #ai.
Other roads do not have a general live travel time lookup; the bot gives a
coverage explanation or asks for a street rather than inventing a report.

<a id="announcement-rules"></a>

## 511 announcement rules

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

Old receipts migrate to fingerprint version 3 silently on the first successful
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
time is checked again before transmission. City polling and freshness are separate.

The existing travel time cache separately checks 511's public tables every five
minutes. It preserves original observation times and marks older reports with
their source times. Ordinary travel time changes never cause announcements.

All channels share the same radio allowance and congestion controls. Traffic
announcements yield to user requests and attempt at most one post per five
second scheduler tick. The radio limiter can impose a longer wait. The first
launch welcome is separate from alert broadcasts. Personality changes, usage tips and fortunes from #ai are not
posted to #traffic.

## Madison closure notices

When Dane is selected, `traffic_madison_enabled = true` (the default) adds
[City of Madison transportation news](https://www.cityofmadison.com/transportation/news).
The reader follows official article links and rechecks previously tracked articles
while they remain relevant. It does not use Twitter, search snippets or the legacy
ArcGIS Road_Closures map: the raw map inspected during implementation contained
records ending in 2021. No model is used for parsing, selection or wording.

The city source selects major downtown streets, approaches and large event notices.
Ordinary lane restrictions and other small closures remain available in `/reports`
but do not automatically broadcast. A phased project is one qualified notice,
not a post per block or a claim that every street stays continuously closed.
A scheduled notice always says "planned"; it is not field confirmation that
barricades are currently in place.

City notices have their own persistent event history, separate from 511 receipt
format migrations. It records the latest facts, publication identities, linked
notices, the last attempted announcement and its reminder state. The rules are:

- Announce a useful first notice even weeks or months ahead (up to 180 days).
- Suppress repeated polling, cosmetic normalization and matching republications
  close together. A new source ID alone is not a new event.
- Allow one reminder when a **new publication** appears within 24 hours of the
  start, and the previous announcement was at least 48 hours earlier. Reaching
  the day before does not itself generate a reminder. A rescheduling to another
  date resets the reminder allowance after its correction is announced.
- Compare changes to the last broadcast, not the last poll. Changes of at least
  30 minutes, changed scope/direction, newly specified or withdrawn start/end
  dates, or changed phase schedules qualify.
  Smaller earlier starts or later endings qualify within 24 hours of the start;
  small less restrictive changes accumulate against the broadcast baseline.
- Explicit cancellations, postponements and reopening notices get a post only
  if the channel previously attempted to announce the original restriction.
  Disappearance and an elapsed estimated end time never create an all-clear.
- An explicit reinstatement or newly published replacement can restore a
  cancelled event. An old cached pre-cancellation article cannot do so.
- Related updates are consolidated before transmission. A cancellation before
  the original is sent results in no post. Partial corrections preserve the
  restrictions that remain, rather than becoming a blanket cancellation.
- Conflicting notices with equal evidence are withheld from broadcasting and
  marked "Unconfirmed" on request until a later update resolves them.

Cross-source suppression is deliberately strict: a city notice and a 511
construction report must agree on an explicit named segment, direction and exact
start/end times, and describe the same full closure. Different hazards or merely
nearby incidents are not merged. Existing 511 receipts can provide a baseline
when their saved fingerprint matches the current report; no old receipt is reset
or retried to establish that baseline. Uncertain matches remain separate.

### Published plans and coverage limits

The parser supports explicit month/day dates, simple clock windows, estimated
month or day/week durations, dated phase/recurring notices, explicit status
headlines, and rescheduling phrased with an original date and a new date.
Simple directional partial cancellations can inherit the known event schedule.
Unclear schedules, unsupported partial-cancellation wording, uncertain directions
and articles with changed markup are omitted rather than guessed. This is not
comprehensive coverage of every city notice. The source link remains available
with `/details`; when the article URL cannot fit, the city news index is
provided. Long announcements are withheld rather than truncated.

Date-only notices never invent an exact starting hour. Multi-phase/recurring
notices explicitly say that times vary. After a known end, a report expires
without asserting reopening. A notice with no known end is retained for at most
30 days beyond its start or publication, whichever is later, unless a fresh
publication extends its relevance. Terminal notices are considered for seven
days after publication.

511 and the city refresh independently in the background every `traffic_refresh_s`
(five minutes by default). Requests read these cached reports without fetching
the source website. City observations and announcement history persist across
restarts, but a fresh check is required before serving them again. A failed source
cannot make another source unavailable or make its own old observations fresh. Broadcasts require a successful check within ten
minutes. Individual failed city articles are excluded from that snapshot and
coverage is marked partial; they do not cancel the saved events. The city serves
cached pages, sometimes roughly an hour old: cache ages above six hours are
rejected, but this cannot provide instantaneous cancellation delivery. The reader
uses at most 60 active/recent article URLs per poll, four concurrent requests,
a 25-second total fetch budget, and a one-megabyte limit per page. Exceeding the
active-article limit fails the city check instead of silently dropping pages.
This budget is independent of, and does not change, the chat generation budget.
The Madison reader identifies itself as Mesh Potato, with its Traffic version and
repository URL, and requests HTML on both listing and article requests.

### Previewing and inspecting decisions

Set `traffic_madison_preview = true` to fetch, save and show city reports while
logging proposed announcements without transmitting them. Existing 511 posts
continue normally. Preview does not mark a notice as sent; turning preview off
allows still-relevant eligible notices to broadcast.

`traffic_notice_decision` log records contain the source, proposed text, and a
reason such as "new notice", "already announced", "minor time change",
"fresh day-before notice" or "cancelled". They are emitted when the inputs or
reason change. `traffic_city_refresh` records these snapshot counts:

- `reports`: currently relevant city events retained after matching and filtering.
- `omitted`: fetched articles the parser deliberately cannot turn into a verified
  traffic notice, for example because dates or directional scope are unclear.
- `unavailable`: article fetch or parsing exceptions. This count alone does not
  distinguish HTTP failures from parser bugs, and does not mean pages are missing.

Expired notices do not count as current reports. `/status` marks city coverage
partial when either `omitted` or `unavailable` is nonzero. Requested reports also
show "Partial coverage" when either configured source is stale or unavailable.
The ordinary `traffic_alert_post` log records actual posting outcomes.
`/status` distinguishes fresh, partial and unavailable city coverage.

## Storage and failures

Announcement receipts are in the #traffic channel's SQLite database, for example
`meshpotato.channel-2.sqlite3`. Back it up with the other channel databases.
Changing a slot's channel identity requires a separate database. A persistent
database is required; disabling persistence would repeat alerts on every restart.

A receipt is saved immediately before the radio attempt, after validation and
rate admission. An ambiguous failed send, interrupted send or restart never
automatically retries that report. It might already have reached the mesh.
People can still request it with `/reports`. Busy channels, expired reports and
failed database writes do not mark an unsent report as delivered.

History is bounded to 10,000 distinct 511 report identifiers and separately
10,000 city events, with at most 200 source aliases per city event. Existing
history is never silently evicted to make room. Corrupt history prevents startup;
a capacity error rejects the affected refresh or update and logs an error.
Existing receipts are preserved for operator recovery.

Mesh Potato Traffic is version **2.0.3**, independent of the services in #chess and #ai. Use `/about` or `/version` to see it. Its first launch welcome explains
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
| `channel.py` | Independent polling, saved announcement receipts, help and report navigation |
| `madison.py` | Bounded official city notice reader and conservative parsing |
| `planned.py` | Publication/event matching, corrections, reminders and durable decision history |

Run isolated tests from the repository root:

```bash
.venv/bin/pytest -q tests/test_traffic.py tests/test_traffic_channel.py tests/test_traffic_notices.py
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
