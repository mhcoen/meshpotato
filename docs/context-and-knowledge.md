# Conversation context and radio knowledge

[Back to the README](../README.md#features) | [Configuration](configuration.md)

## Conversation context

The model receives its fixed system rules and one user message containing the
current question, its reception measurements, any selected radio references, recent personal exchanges, and
recent channel history, in that order. Conversation and reference material are
background, not instructions, and never become system-prompt text.
The bounded [reception block](reception.md) describes only the current delivered
question. It is captured before queueing and included in the full context gate.

Personal memory keeps complete question/answer pairs, trimming oldest pairs to
`person_memory_max_chars`. Matching channel lines for those included pairs are
then omitted from the channel block, before its own `transcript_max_chars` trim.
Matching includes the sender and exact reply mention; another person's identical
words are not removed. Trigger prefixes are accounted for. An exchange that
does not fit personal context remains eligible for channel context.

Queued questions keep the channel snapshot from their arrival, so later channel
conversation cannot become earlier background. Personal memory is refreshed at
admission, and overlap removal runs again. `/forget` still clears personal memory
immediately and prevents older in-flight requests from restoring it; it does not
erase shared history or logs.

This reduces duplicated examples that can encourage parroting, but does not
guarantee novel answers, and small models copy their own earlier replies out of
these blocks regardless of instructions. A reply check on the shaped output
catches a repeat of a recent reply, an echo of the message, a `@[` mention, a
joke at the person asking, or a radio metaphor on a question that is not about
radio, gives the model one retry with the problem spelled out, and otherwise
sends nothing; see the README's message handling steps for what the two content
checks look for and what they cannot see. There is no generated
summarization; the retry is one more generation, and each generation can carry
its own shortening retries.
Existing memory population, age, and size limits remain unchanged. Conversation
state is backed by a local SQLite file and survives restarts; restored text is
re-checked by the injection gate. Channel history additionally expires after one
hour by default. See [Conversation storage](storage.md). Optional logs are separate.

## Offline radio references

[The bundled corpus](../bot/radio_reference.toml) contains short, reviewed
paraphrases about spreading factor, bandwidth, coding rate, RSSI/SNR, MeshCore
routing, and room servers. Each passage has topic keywords, a source URL, and
a review date. Its sources are Semtech's LoRa Modem Design Guide, the MeshCore
FAQ, and The Things Network's RSSI/SNR documentation; links are in the corpus.

Selection uses radio-specific words and phrases in the current question, ranked by the
number of matching keywords. At most two complete passages are included, with a
combined limit of 1200 characters including titles and source links. No match
means no reference block. The bot never fetches these URLs, downloads documents,
or creates a database. A vague follow-up without a topic keyword relies on the
existing conversation context and built-in facts, not fresh retrieval.

Selected passages are included in the assembled-context injection checks before
admission and again before generation. They stay present during shortening
retries. Reference-derived answers use the same one-packet size checks, outbound
gate, queue, and airtime limiter as other answers.

These are general technical references, not live mesh telemetry. Companion radio
settings are a startup snapshot; local Madison/operator information continues
to come from the existing `facts` config field. The bot cannot infer a remote
node's current settings, status, or a guaranteed range from reference material.
General facts, the companion's startup settings, and operator notes are labelled
separately in the system prompt. The general facts and startup settings are
included only for a question that mentions radio; a note on how the bot works
and the operator notes are included for every question.
It still generates its answer with an LLM and can get things wrong.

## Maintaining the reference collection

Edit `bot/radio_reference.toml` in a source checkout and restart the bot; for a
wheel installation, rebuild and reinstall the package. No channel command can
edit this file, choose another file, or change the system prompt. Keep local
deployment notes in `config.toml`'s `facts`, separate from general references.

Each `[[reference]]` table needs `title`, `keywords`, `text`, `source`, and
`reviewed` (an ISO date string). Use short, printable ASCII prose, explicit
tradeoffs, and primary sources. Recheck the source and update the review date
when changing a passage. Distinguish LoRa modulation facts from LoRaWAN-specific
protocol behavior; MeshCore is not LoRaWAN.

The loader rejects malformed entries, non-ASCII text, oversized passages, more
than 32 passages, or a file exceeding 64 KiB. Before opening the radio, startup
also checks every passage alone and in its context framing with the configured
injection threshold. A flagged passage or detector failure produces a clear
`config error` naming the passage; it is not deferred to channel questions.
The same validated corpus is passed to the service without rereading the file.
Runtime context checks still cover combinations with questions and history that
cannot be exhaustively checked at startup. Sources are checked during maintenance,
not on a runtime schedule. Run `.venv/bin/pytest` after editing.
