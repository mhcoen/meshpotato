# Conversation storage

[Back to the README](../README.md#per-person-memory) | [Configuration](configuration.md)

Mesh Potato saves recent channel history and per-person conversations in
`meshpotato.sqlite3`. SQLite is included with Python; no separate database
server, account, or network connection is needed. The file is created on
startup, after the bot verifies the radio and channel, before it starts
fetching messages.

## What survives a restart

- Recent channel lines: at most `history_size` (20), no older than
  `history_max_age_s` (3600 seconds, or one hour).
- Per-person answered exchanges: at most `person_memory_rounds` (20) per name,
  no older than `person_memory_days` (14), for at most `person_memory_people`
  (500) names. Least-recently-used order survives too.
- Original question text, exact Unicode sender names, and timestamps, so
  context deduplication and age limits still work after restarting.

Age is measured using wall-clock timestamps, not uptime. Time spent shut
down counts toward expiration. A backward clock correction does not erase live
history. On restore, future timestamps are clamped to the current time so recent
conversations survive and age normally afterward; already-expired entries are
still dropped. Keep the computer clock correct.

Queued questions, rate-limit tokens, persona switches, and fortune schedules
are not saved. Restarting does not replay old requests or transmit old replies.
The injection gate checks restored text again, and assembled model context
still goes through its normal checks. Flagged ingestion lines are not saved.
A detector failure during restore stops startup and preserves the database.

## Save timing and /forget

A bounded snapshot is saved every five seconds and on clean shutdown.
Each update is one SQLite transaction: a failed write does not replace the
previous snapshot with half an update. A crash or power loss may lose changes
since the last successful save; the database is not a full archival chat log.

`/forget` removes the sender's personal memory and saves immediately, before
waiting for a radio token for its confirmation. Earlier queued or generating
requests cannot repopulate that person's memory. Shared channel history and
separate JSON logs are not erased by `/forget`.

Save errors appear as `state_error` in the log and terminal monitor. The bot
keeps running with its in-memory context and retries at the next save interval.
If saving `/forget` fails, no success confirmation is transmitted. Changes made
while storage is unavailable may be lost on restart.

## Configuration and maintenance

The `[history]` section of `config.toml` contains the storage settings. A
relative `state_db` path is relative to the bot's working directory, not the
config file. Use an absolute path if a service may start in different directories.
The parent directory must already exist. Set `state_db = ""` to keep state
only in RAM; this does not delete an existing database.

Use a separate file per bot/channel. The database records the bot name and
channel identity and refuses a mismatch rather than mixing conversations.
It also detects competing writers rather than silently overwriting their
snapshots. An invalid, unsupported, or inaccessible database produces a clear
startup error; the bot does not discard it or quietly start with empty memory.

Retention limits are applied on restore and at each save. Expired rows are
removed from the database, and SQLite reuses freed pages; the file may retain
its previous high-water size. Database files and SQLite sidecar files are
ignored by Git.

For a backup, stop the bot cleanly and copy the database. To start fresh, stop
the bot and move the database aside; startup creates a new one. Do not replace
or copy a live database file as a backup: use SQLite's backup facility if the
bot must stay running. Old backups retain the conversations they contained,
including any personal memory forgotten since the backup.

## Channel service state

Each channel has its own database. The primary channel uses `state_db`; additional
slots use neighboring files such as `meshpotato.channel-2.sqlite3`. Back up all
of them when preserving AI, chess and traffic state together.

The existing database format gains tables without discarding saved conversations:

| Table | Saved state |
| --- | --- |
| `usage_tips` | Usage example rotation and attempted scheduling slots |
| `chess_games` | One game per sender name, full move history, restart confirmation and recent packet receipts |
| `traffic_state` | First feed initialization, versioned alert fingerprints, derived hazard/closure/lane facts and claim times |
| `startup_welcome` | Whether this channel has attempted its first welcome |

Chess commits a completed turn before attempting its reply. Traffic and welcome
receipts are committed immediately before a radio attempt, after admission and
content checks. An ambiguous send failure does not erase these receipts or cause
a retry after restart. Database generation checks apply to these writes too.
Chess and traffic require persistent storage.

Traffic reports rejected before transmission are remembered only in memory.
They may be checked again after restart, but do not block other eligible reports.
The live feed snapshot and current alert browsing position are not persisted.
Moving a database aside also resets its welcomes, alert receipts and saved games;
do not discard it simply to restart the program.

Traffic fingerprint version 2 migrates older receipts lazily by identifier when
that report next appears in a successful poll. The new facts are saved silently,
keeping the previous claim time. An unseen identifier is not baselined during
migration. Changed reports normally wait 30 minutes between attempts, except
for closure status changes, hazard set changes or an increase in blocked lanes.
The interval and exception facts survive restart. A deliberately suppressed
initial backlog is marked as unattempted, so its first later material change is
not subject to a repeat interval for a transmission that never happened.
