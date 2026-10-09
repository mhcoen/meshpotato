# Mesh Potato manual

[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](../LICENSE)
[![Tests](https://img.shields.io/badge/tests-pytest-green.svg)](#development)

A chat bot for a [MeshCore](https://meshcore.co.uk) channel. Mesh Potato runs on a
computer with a MeshCore companion radio on USB, listens on configured channels,
answers questions using a language model running on the same computer, and
posts a one sentence reply back to the channel as `@[sender] answer`. Every
message and every reply passes a built-in prompt injection detector before it
can reach the model or the radio.

It is a small Python package with no web interface or database server.
A local SQLite file keeps recent conversations across restarts.

This manual covers the #ai bot and shared program in full; the [front page](../README.md) is the short version. Each additional service has
its own instructions: [#chess README](chess/README.md) and
[#traffic README](../bot/traffic/README.md).

A live instance runs on the `#ai` channel of the MeshCore mesh in
southern Wisconsin, centered on Madison. If you are on that mesh, add `#ai`
in your MeshCore app and say something. It targets about 2 percent of the
channel's time for its own transmissions, never answers closer than 15 seconds
apart, and backs off when the channel is busy. Questions wait in a bounded
queue, so a delayed answer can mean congestion rather than a fault.

## Screenshots

The terminal monitor shows shared radio status and separate channel panels.
These screenshots use illustrative messages, not live reports:

![The Mesh Potato terminal monitor with three channel columns](tui.svg)

Narrow terminals use tabs:

![The Mesh Potato terminal monitor with channel tabs](tui-narrow.svg)

## Features

- Answers questions on one or more MeshCore channels, skipping bare reactions and
  lines addressed to someone else, and staying out of conversations between
  other people. On a shared channel, an optional trigger prefix such as `!ai`
  limits it to messages meant for it
- Per-person memory of recent exchanges, preserved across restarts so follow-up questions make sense.
  Overlapping exchanges appear only once in model context; `/forget` clears
  your personal memory, not the shared channel history
- Optional [#chess channel](chess/README.md), with Stockfish, saved games per player,
  adjustable difficulty, legal-move checks, hints, draw handling and restart recovery.
- Optional [#traffic channel](../bot/traffic/README.md) for important Wisconsin 511
  reports, with help, current alert lists, requested travel times and saved
  announcement history across restarts.
- A bundled [abbreviated README](../bot/README.short.txt) in every model request,
  with facts from the running configuration about commands, capabilities, memory,
  privacy and limits. Common questions about sports support, model identity and
  upgrades, voice expiry and reply length get program-generated answers.
- Talks LoRa, not information theory. It knows the mesh's settings and what
  each one trades off, reads its own frequency, bandwidth, and power from the
  radio at startup, and selects relevant passages from a small
  [sourced, offline radio reference](context-and-knowledge.md)
- Local model through Ollama, or any OpenAI compatible chat endpoint
- Ask "How did my message reach you?" for an explanation using that question's
  reported hop count, RSSI and SNR, when available; see [Reception](reception.md)
- Warm, helpful answers by default with `/nice`. Other personalities are by
  request: `/funny`, `/snarky`,
  `/marvin`, `/pirate`, `/haiku`, `/serious` (straight answers without jokes),
  with `/help` and `/reset`. A switch reverts
  after two hours. Presets live in `config.toml`; write your own
- A daily fortune, silly and unprompted, a little after six every morning,
  independent of the channel's current personality
- Automatic web lookup for current-information questions, plus `/web` to request
  a search; one short answer with a source domain, or an honest inability to verify
- Prompt injection gate on every channel line, the prompt, the assembled
  context, and the reply
- Polite on the air: a target share of channel time for its own
  transmissions, measured from the radio's airtime counters, and automatic
  slowdown when the channel is busy

## Also

- One sentence ASCII answers, automatically sized to fit the radio's
  160-byte limit including the node name and mention; sender names are
  mentioned exactly as sent. Structured weather summaries can include weather
  icons and degree symbols within the same byte limit
- Loop guard, prompt length cap, hard model timeout with a fixed apology
- Rate limits, global and per sender, as a burst floor; up to ten waiting
  questions and one active answer, with waiting work expiring after ten
  minutes
- Recent channel history given to the model as untrusted background, never
  as prior chat turns
- Terminal monitor with a live message log, rate limiter state, channel
  utilisation, and counters; JSON lines log; headless mode for services
- Introduces its name, version, capabilities, and help on first launch, remembering
  the attempt across restarts when persistence is enabled; clean shutdown on SIGINT
  and SIGTERM
- Tests that need no radio, no model, and no network

## Channel commands

Just send a message to chat; no command is needed. Both `!` and `/` commands work,
including `!help`, `/help`, or plain `help`. "What can you do?" also shows examples.
A configured custom command prefix works too. With the default presets:

| Command | What it does |
| --- | --- |
| `!help` or `/help` | Shows enabled examples; points traffic questions to #traffic when that channel is configured |
| `!help topics` or `/help topics` | Lists command help topics and `about` |
| `about`, `!about` or `/about` | Shows version, configured model and host/radio arrangement, plus the GitHub README link when the complete reply fits |
| `!about source` or `/about source` | Gives the source repository link |
| `!weather` or `/weather` | Gets the daily weather; `!wx` and `/wx` also work, with an optional location |
| `!traffic` or `/traffic` | Directs users to #traffic when configured; otherwise uses the traffic lookup in #ai described below |
| `/help web` | Automatic searches and `/web` usage; the bot handles the internet connection and search |
| `/help voices` | Lists the configured voice commands, without descriptions |
| `/help fun` | Dice and Magic 8 Ball examples, plus daily fortunes when enabled |
| `/help privacy` | Explains what `/forget` clears and what `/reset` changes |
| `/serious` | Straight, factual answers without jokes |
| `/nice` | Warm, helpful answers; the default voice |
| `/funny` | Dry humor, by request |
| `/web question` | Search the web and summarize available evidence in one sentence |
| `/snarky` | Sharp, unimpressed humor |
| `/marvin` | A brilliant, deeply depressed robot |
| `/pirate` | A cheerful pirate |
| `/haiku` | Answers as a one-line haiku |
| `/reset` | Restores the default personality immediately |
| `/forget` | Clears the bot's personal memory of you, not shared channel history |
| `/roll` | Rolls two six-sided dice by default; accepts a dice count and sides per die, e.g. `/roll 3 8` or `/roll 3,8` |
| `/magic8` | Gives a random Magic 8 Ball answer; an optional yes/no question can follow |

Personality switches are silent, affect the whole channel, and revert after
two hours by default. If a trigger prefix is configured, put it before the
command, for example `!ai /help`. See [Personalities](#personalities) for
configuration, command details, and limits.

## Twice-daily usage tips

The bot offers two short "Potato tip" examples per day, including **weather,
sports, and traffic**, plus radio questions, poems, jokes, translations, games,
and help. These are reviewed examples of what to ask; conversational answers
still come from the model or live-data handlers. Morning tips rotate through
weather, sports and traffic; evening tips cover the broader set of uses. If web
lookup is disabled, both slots use the non-web examples. When #traffic is
configured, traffic question tips are omitted from #ai and the introduction tip
points to #traffic instead.

Defaults are **10:00 a.m. and 6:00 p.m. in the host computer's local timezone**,
with a random offset of up to five minutes. Each tip is one packet, needs no
model call or web lookup, and uses the normal global/sender airtime limits. It
waits for 60 seconds without activity on the served channel, yields to active
or queued requests, and only sends when adaptive utilization permits full rate.
If no quiet opportunity appears within 15 minutes, that tip is skipped. An
expired slot is never caught up after startup or sleep, and a radio attempt is
never retried when delivery is uncertain. The morning fortune remains separate.

Configure `[tips]` with `tips_enabled`, `tips_morning_time`,
`tips_evening_time`, `tips_jitter_min`, `tips_window_min`, and `tips_quiet_s`.
The two times must be ordered and at least an hour apart. Set `tips_enabled = false`
to disable announcements. The normal state database remembers attempted slots
and used examples across restarts; disabling `state_db` makes that memory last
only for the current process. Tip state is separate from personal conversation
memory. State-write failures skip the tip instead of risking a duplicate.

Each topic pool uses its eligible examples before recycling, then avoids up to
ten recent examples in that pool (always leaving at least one available). Web-dependent examples are omitted when web lookup is disabled;
command and trigger examples reflect the configured prefixes. Examples that
cannot fit a custom radio identity are omitted. Data-dependent questions may
return a temporary-unavailability notice when their sources are unavailable.

<details>
<summary>All 68 usage-tip examples (default configuration)</summary>

1. Potato tip: Ask about weather, sports, traffic, radio, or something fun. Try "What can you do?"
2. Potato tip: Try "weather" for local conditions and tomorrow's forecast.
3. Potato tip: Try "What is the traffic on the Madison Beltline?"
4. Potato tip: Try "What is the Packers record?"
5. Potato tip: Try "Write a tiny poem about cheese."
6. Potato tip: Try "What does SNR mean?"
7. Potato tip: Traveling? Try "weather in Chicago, IL". Name a city for another location.
8. Potato tip: Try "What is the Brewers record?"
9. Potato tip: Try "Madison Beltline eastbound delays?" for the eastbound travel-time report.
10. Potato tip: Try "Tell me a short joke about geese."
11. Potato tip: Try "What does RSSI mean?"
12. Potato tip: Try "How do you say good night in Spanish?"
13. Potato tip: Try "weather in Verona, WI" for a nearby forecast.
14. Potato tip: Try "When is the Brewers next game?"
15. Potato tip: Heading west? Try "Madison Beltline westbound traffic?"
16. Potato tip: Try "Write a tiny poem about the moon."
17. Potato tip: Try "What does spreading factor change?"
18. Potato tip: Try "Explain gravity like I am ten."
19. Potato tip: Try "weather in Milwaukee, WI" before a trip.
20. Potato tip: Try "When is the Packers next game?"
21. Potato tip: Beltline reports show their age. Try "Madison Beltline traffic?" Stale data is not called current.
22. Potato tip: Try "Tell me a potato joke."
23. Potato tip: Try "How does LoRa bandwidth affect range?"
24. Potato tip: Try "How do you say thank you in French?"
25. Potato tip: Short on typing? Try "wx" for a local weather summary.
26. Potato tip: Try "What is the Niners record?" Nicknames can save typing.
27. Potato tip: Traffic questions can use ordinary words: "Any delays on the Madison Beltline?"
28. Potato tip: Try "Write a tiny poem about a sleepy dog."
29. Potato tip: Try "What is LoRa coding rate?"
30. Potato tip: Try "Why is the sky blue?"
31. Potato tip: Try "weather in Middleton, WI" for conditions and a forecast.
32. Potato tip: Try "When is the Bucks next game?"
33. Potato tip: Try "What is the Brewers record and when is their next game?"
34. Potato tip: Try "Tell me a short joke about a rubber duck."
35. Potato tip: Try "What does a MeshCore repeater do?"
36. Potato tip: Try "How does a rainbow form?"
37. Potato tip: Try "weather in Madison, WI". Weather summaries include today and tomorrow.
38. Potato tip: Shared team names need context. Try "What is the SF Giants record?"
39. Potato tip: For an older score, include the team and an actual game date written as YYYY-MM-DD.
40. Potato tip: Try "Write a tiny poem about rain on a roof."
41. Potato tip: Try "What does antenna gain mean?"
42. Potato tip: Try "Why does the moon have phases?"
43. Potato tip: After asking about a team, try "When is their next game?" I remember recent team context.
44. Potato tip: For ambiguous team names, include the sport or league: "New York Giants NFL record?"
45. Potato tip: Try "Write a tiny poem about messages crossing the night."
46. Potato tip: Try "Why does antenna height matter?"
47. Potato tip: Try "What is 15 percent of 80?"
48. Potato tip: Try "How do you say welcome in German?"
49. Potato tip: Try "Make this friendlier: Please stop blocking the driveway."
50. Potato tip: Try "Suggest three names for a robot potato."
51. Potato tip: Try "How does MeshCore recognize duplicate messages?"
52. Potato tip: Try "What causes thunder?"
53. Potato tip: Try "How many miles is 10 kilometers?"
54. Potato tip: Try "Translate bonjour into English."
55. Potato tip: Try "Shorten this: I am on my way and should arrive in about ten minutes."
56. Potato tip: Try "Suggest three picnic snacks."
57. Potato tip: Try "What is the difference between RSSI and SNR?"
58. Potato tip: Try "Why do we have seasons?"
59. Potato tip: After an answer, try "Explain that more simply." Short follow-up questions work.
60. Potato tip: After an explanation, try "Give me an example."
61. Potato tip: Try "What can you do?" for a short introduction.
62. Potato tip: Curious about the bot? Try "What model are you?"
63. Potato tip: For commands and help topics, send "/help".
64. Potato tip: For a dice roll, send "/roll 2 6". That rolls two six-sided dice.
65. Potato tip: For a playful yes/no answer, send "/magic8 Should I have another cookie?"
66. Potato tip: Try "/help privacy" to learn about memory. This is a shared radio channel.
67. Potato tip: Try "/forget" to clear my personal memory of you. Shared channel history and logs remain.
68. Potato tip: Try "/help voices" to see styles. Voice changes affect the whole channel.

</details>

## What you can ask

Ask a question in ordinary language. For example:

- "What does spreading factor change?"
- "How did my message reach you?"
- "Why is the sky blue?"

The bot remembers your recent exchanges, so you can follow up with questions
such as "Can you explain that more simply?" Answers are kept to one short
sentence to fit a radio message. Use `/serious` for straight answers without
jokes, or `/help` to see the available commands. Personality changes affect
everyone on the channel, not just the person who requested them.
You can also use your app's reply button to reply to Mesh Potato; a leading
`@[Mesh Potato] ` mention addresses it directly, even on a channel with a trigger
prefix. To prevent two bots from replying to each other indefinitely, the bot
answers six consecutive direct-mention requests from one sender, then sends one
short notice: "Loop limit: send a new message or wait a minute to continue."
Further direct mentions are suppressed until that sender pauses for a minute
or sends a plain message. On a channel with a trigger, include that trigger.
The same limit covers command and live-data replies, including queued requests.
An unknown command receives one short help hint.

When complete, validated scoreboards contain no matching game for a team and
date, the bot says so and suggests specifying a game date or asking for the next
game. A failed or incomplete lookup still gets a temporary-unavailability notice.

Questions about sports scores, current prices, weather (including `wx`), road traffic, opening hours, news, and similar
changing facts trigger a web lookup. Use `/web your question` when you want to
request a search explicitly. For example, `/web How much is an 8-foot treated
4x4 at Menards Madison East today?` searches for current supporting pages.
Words such as "today" or "tonight" alone do not trigger search. Ordinary
requests such as "What should I cook tonight?" and "Who is W1MHC?" stay with
the model and configured local facts. Include the exact product and store location; sites that require a login,
JavaScript, or a bot check may prevent the bot from finding an answer.

For daily weather, send `wx`, `Weather`, or `weather in Chicago, IL`.
The default location is `web_location`; a city and state or country in the request
overrides it. Ordinary current/tomorrow summaries use structured
[Open-Meteo forecast data](https://open-meteo.com/en/docs) and its
[geocoding service](https://open-meteo.com/en/docs/geocoding-api), without a model
call. Example format (illustrative values, not a current forecast):

> 🌤️ Daily Weather: Madison, WI: ☀️Clear 55°F WNW5mph | H:73°F L:55°F | Tomorrow: ☁️Overcast H:65°F L:48°F

These are model-based weather estimates and forecasts. The summary uses Fahrenheit
and mph, and dates in the requested location's timezone. If the full presentation
does not fit after the sender mention, it shortens labels and removes decoration
before dropping any weather fields; it never splits a summary across packets.
Ambiguous place names receive a clarification. Missing, malformed or stale weather
data receives a notice that live weather lookup is supported but unavailable at
the moment. Sports and traffic lookup failures use the same topic-specific wording,
for example: "I can look up live traffic, but can't get it right now. Please try again."
This does not assert a particular cause or a permanent loss of the feature;
disabled lookup and ambiguous requests keep their separate explanations.
The formatter checks units, current
timestamps, and both forecast dates; retained weather expires after at most 15
minutes and at local midnight. Specific questions outside a daily summary still
use general web lookup. Weather provenance is recorded in `weather_lookup` logs.

For Madison Beltline and I-90 traffic, the bot refreshes the public
[Wisconsin 511 travel-time table](https://511wi.gov/list/traveltimes) at startup
and every five minutes in the background. No API key is needed for these travel
times. When the dedicated #traffic channel is enabled, all traffic answers and
announcements go there; the #ai channel directs traffic questions to #traffic. Help in #ai
names that destination, and welcomes and usage tips in #ai stop inviting traffic
questions on #ai. Traffic broadcasts compare derived incident facts,
so cosmetic source edits stay quiet; repeat controls and receipt migration are
documented in the traffic README.
Setup, help commands, current alert browsing and announcement rules are in the
[#traffic README alongside its code](../bot/traffic/README.md). Questions such as
"What is the traffic on the Beltline?" and "Beltline eastbound delays?" use a
prepared report for the University Avenue–I-39/90 corridor, without a foreground
search or model call. In #ai, these requests redirect when #traffic
is configured. The examples and general web behavior below describe traffic
lookup in #ai when the dedicated channel is disabled. #traffic uses the same corridor
cache without source labels and does not use general web search for other roads.
`/web Beltline traffic?` also uses the cache in #ai in that standalone setup. Example with
**illustrative measurements**:

> Beltline: Eastbound 22 min, 5 min delay (2 min ago). Westbound 17 min, no delay (2 min ago). Source: 511.

"Traffic on I90", "I-90 northbound traffic", and `/traffic I-39/90` use measured
travel times **between the Beltline and I-94 (Badger Interchange)**. This is a
specific Madison corridor, not a report for the entire interstate. For example,
with **illustrative measurements**:

> I-90 Beltline to I-94: Northbound 4 min, no delay (2 min ago). Southbound 8 min, 5 min delay (2 min ago). Source: 511.

If both dated directions do not fit one radio packet, the bot asks for northbound
or southbound. Explicitly named destinations outside this corridor, other roads,
incidents and forecasts use general lookup, never these corridor measurements.
The two table searches refresh together; a failed search preserves its previous
measurements while the other can still update.

"Traffic downtown" now answers:

> Downtown Madison: which street and direction? I can check reports, but have no downtown-wide live traffic feed.

A road-name follow-up such as "John Nolen Drive northbound" within two minutes
becomes a traffic lookup for that sender on that channel. `/traffic John Nolen
Drive northbound` works directly. If the lookup cannot verify conditions, it says:

> I couldn't verify traffic for that road. Try a road, direction and nearby exit, or check 511wi.gov.

This distinguishes an unverified road from a temporary failure of a supported
cached report. A broad "How's traffic?" asks which road and direction first.

Travel times and additional delays are in minutes. Each direction shows its
own source update age or time; clock times are Central time. Only source measurements and successful
fetches under ten minutes old are described as current. Older reports remain
available with their original source times in parentheses, for example:

> Beltline: Eastbound 17 min, no delay (yesterday 11:06 PM). Westbound 17 min, no delay (4:13 AM). Source: 511.

Replies say "yesterday" or give a date for older measurements.
A failed refresh preserves the last known measurements without changing their
times. If only one direction is available, the reply identifies the missing
direction. The unavailable notice is reserved for missing or invalid data, or
a report that cannot fit in the radio message. The travel time cache never broadcasts unsolicited reports; the dedicated
#traffic service separately announces significant alerts. Normal reply spacing
and congestion limits still apply.
Specific exits, incidents, closures, other roads, and future traffic questions
continue through general web lookup. Bare Beltline and I-90 requests use this cache only
when `web_location` is Madison; explicitly naming Madison works from other defaults.
Set `traffic_enabled = false` to disable prefetching, or change
`traffic_refresh_s` (default `300`, allowed `60`–`600`). Disabling `web_enabled`
also disables prefetching when only #ai is configured. A configured #traffic channel
can still run its cache with web search disabled. `traffic_enabled = false`
disables the travel time cache, not authenticated incident alerts. This public
table is a website interface,
so a site format change can make the cache unavailable until its parser is updated.

For sports, ask "What's the score of the Packers game?" or use
`/web Packers score`. NFL, NBA, WNBA, MLB, and NHL scores come directly from
ESPN's structured scoreboards. The reply includes both teams and scores, game
status (quarter/period/inning, halftime, final, scheduled, or postponed), game
date, and the local time the bot fetched the ESPN snapshot. The model does not
invent, infer, or rewrite scores. For example, the format is
`Packers 19, Vikings 10; Q3 10:55, 09/13 (ESPN 22:16 CDT).`

"Packer game" also means the Green Bay Packers; for example, "What was the
score on the Packer game tonight?" uses today's game in the bot's local timezone.
Common nicknames work for scores, records, standings and schedules: Pats, Niners,
Pack, Yanks, BoSox, ChiSox, Sixers, Cavs, Habs, Pens and others. `SF Giants`
explicitly selects San Francisco. Shared names such as Giants, Sox, Jets and
Bolts need a league, a city, or an established team context; the bot asks when
ambiguous rather than picking whichever team happens to have a game that day.

Sports questions also recognize standings and schedules:

| Ask | Answer uses |
| --- | --- |
| "What place are the Brewers in?" / "Where do the Cubs stand?" | Current division position and win-loss record |
| "How are the Packers doing?" / "Brewers record?" | Season standing and record; add "in the game" for the score |
| "Who's leading the NL Central?" / "Who leads the AFC North?" | A leader in that division's standings, with ties identified |
| "Who's leading the American League?" / "Who leads the NL?" | A leader across the named baseball league, with ties identified |
| "How many games behind are the Cubs?" / "How far back are the Brewers?" | The provider's games-behind figure and position |
| "When do the Brewers play next?" / "Who are the Bucks playing next?" | Opponent, home/away order, date and local start time |
| "Did the Brewers win?" / "Are the Packers winning?" | The dated game's actual score and status |
| "Brewers record and next game?" | Both the record and next scheduled game; an unavailable part is explicitly marked unverified |

Standings use the named division or conference. For example, "What place are the
Brewers in the National League?" uses the whole NL table. Position is calculated
from win percentage (hockey points), then games behind when supplied for that
group. Equal statistical positions say "tied"; the bot does not claim to resolve
official playoff tiebreakers or present a conference playoff seed as a division
position. The feed's season dates must include today; an offseason final table
is not presented as current. A team with no completed games recorded gets that
notice instead of an artificial position or games-behind figure. Games-behind figures are included when
the feed supplies them; hockey uses points, and missing metrics are identified
instead of calculated from unrelated fields. Historical standings snapshots are
not supported. Next-game answers require a future scheduled game with a confirmed
start time. If the provider's compact next-game field still names a finished
game, the bot checks small daily scoreboards for the next two weeks, staying
within the same retrieval budget. It refuses an unverified time or opponent.

After a successfully sent team answer, the same sender can ask "When do they play next?"
or "Who's leading the division?" for `history_max_age_s` (one hour by default).
Only team identity is retained; each answer still fetches fresh evidence.
Explicit team or division names take precedence. A next-game question with only
pronouns and no retained team gets a clarification instead of an invented schedule.
Explicit sports questions without a clear team ask for clarification. The bot
does not borrow another sender's topic. This short sports context is held only in
memory, is bounded by `person_memory_people`, and is cleared by `/forget` or an
unsuccessful sports lookup. Failed sends do not establish a new team context.
Ordinary questions such as "How is Michael doing?" or "What is a standing wave?"
remain ordinary conversation. Common-word nicknames such as Heat or Sun need
capitalization, a full team name, or a sports term such as NBA. Abbreviations such
as NO or MIN must be uppercase so ordinary words do not identify another team.

The bot knows that it provides sports and web lookups. Sent lookup answers are
kept in the same channel history and per-person memory as ordinary replies, so
people can refer back to them. A question waiting in the queue also sees bot
answers completed during its wait. Earlier results describe what the bot reported
then; they are not proof of a current score, price, or other changing fact.

Score requests default to games dated today in the bot computer's timezone.
You can specify `yesterday`, `tomorrow`, or an ISO date such as
`/web Packers score 2026-09-13`. Include the league or opponent if the team name
is ambiguous; multiple matching games require clarification. Other date formats,
unsupported leagues, missing games, and unavailable feeds produce an honest
inability notice. Scheduled games show their start time instead of zero scores.
Scores are snapshots reported by ESPN, whose feed can lag; the displayed fetch
time is not a promise of the provider's update time. Each request fetches again,
unchanged scores may be repeated, and a snapshot held more than 60 seconds is
dropped with `stale-sports-score` instead of transmitted. No paid API key is
needed. The same 12-second retrieval cap, 25-second overall budget, radio rate
limits, and outbound content checks apply. A model outage does not block scores.

For other web questions, the bot reads up to three public pages and produces one short sentence with
the registrable source domain (for example, `example.co.uk`, without subdomain
text). Hostnames containing recognized abusive phrases are rejected. Full source
URLs and supporting quotations are recorded in the local JSON log. An explicit
`/web` with no usable evidence returns a fixed inability-to-verify response.
An automatic lookup with no evidence can use the remaining budget for a normal
model response with a no-current-facts instruction. Only an unchanged static
operator fact can be used from that fallback; other candidates become the fixed
inability line, so adding "I can't verify" cannot sneak a current claim through.
Fixed web notices may repeat, subject to the normal radio rate limits.
The terminal monitor and JSON log include `sports_lookup`, `web_lookup`, source
rejections, retries, and budget exhaustion so lookup failures can be diagnosed.
Rejected web-answer drafts are retained on `web_retry` records for diagnosis;
they are not replies sent to the channel.
Search snippets alone are never supporting evidence.
Current price answers require a page with product/offer metadata; a fetched
listing still does not prove local stock or the price at a particular store.

General web lookup uses the free DuckDuckGo backend of DDGS and extraction adapted from
Episodic's Muse mode, without installing Episodic. It requires internet access
on the bot's computer. For general searches, the current question is sent to DuckDuckGo, and the
result pages are fetched directly. Sender names, channel history, radio keys,
and operator notes are not added to the search query. Anything a person puts
in their question can leave the mesh. The bot's computer handles the internet
connection and search; the person asking needs no internet access, account, app,
or extra setup. The `/help web` topic makes this clear. Set `web_enabled = false`
to disable web lookup, including scores and weather. Weather summaries send only
the requested/default location to Open-Meteo geocoding and the resolved coordinates
to its forecast endpoint. Sports lookups request ESPN team catalogs, standings, and dated
scoreboards and match team names locally; they do not send the question to a
search engine. ESPN's public endpoint is an external dependency and may change.
Its JSON requests use a compatibility user-agent identifying Mesh Potato,
verified HTTPS, public-address pinning, a 1 MB cap, and no redirects; cached
responses reporting an age over 60 seconds are refused.
`web_location` defaults to Madison, Wisconsin and supplies a location for local
weather/hours questions that omit one; specify a location in the question to
override it. General search dates use the bot computer's local timezone; structured
weather uses the requested location's timezone.

Search and all model calls share the same **25-second total budget**. Retrieval
gets at most 12 seconds of that budget; it does not receive a fresh model budget
afterward. A draft that fails source-quote, number, or qualifier checks gets one
repair attempt within the same deadline, then an inability-to-verify response.
Checks require supported content words, matching currency and unit types,
named places/stores in the source, and preservation of recognized qualifiers
across the fetched text. They reject conflicting displayed prices and obvious
dry-versus-rain forecast conflicts; forecasts need a matching date and uncertainty
language. These conservative checks can reject useful pages, especially ones
with multiple prices or unrelated sale/holiday wording. They do not prove
semantic accuracy, freshness of undated listings, or general agreement between
sources. Rejected sources and worker failures have separate JSON log records.
Automatic routing is heuristic; `/web` handles questions it misses.

On a busy channel, allow time for a reply before repeating your question.
The bot spaces out its transmissions and may leave ordinary chatter or
conversations between other people unanswered.

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

The radio needs the MeshCore companion USB firmware, a node name equal to
`bot_name`, your regional radio preset, and the channel it will serve. See
[Prepare the radio](#prepare-the-radio).

## Requirements

- A Mac or Linux computer that stays on. This is a good use for an old
  laptop that is sitting in a drawer: the bot needs no screen once it is
  running, and a reply every 15 seconds at the very most is not much work. The default model
  uses about 18 GB of memory; 32 GB of RAM is a comfortable minimum, 64 GB is
  better. Apple Silicon works well.
- A MeshCore companion radio on USB. Built and tested with a Heltec Wireless
  Paper (ESP32-S3, SX1262) on MeshCore companion firmware 1.17.1. Any board
  with a MeshCore "companion radio USB" build should work.
- Python 3.11 or newer, and git.
- [Ollama](https://ollama.com), or any server that speaks the OpenAI chat
  completions API.

## Installation

The steps below use [uv](https://docs.astral.sh/uv/). Plain `python -m venv`
and `pip` work the same way; pip equivalents are given where they differ.

1. Install uv if you do not have it:

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Get the code:

   ```bash
   git clone https://github.com/mhcoen/meshpotato.git meshpotato
   cd meshpotato
   ```

3. Create the environment and install:

   ```bash
   uv venv --python 3.12
   uv pip install -e '.[dev]'
   ```

   With pip:

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -e '.[dev]'
   ```

   Dependencies include meshcore, ollama, httpx, textual, the web retrieval
   libraries and confusables, which supplies the Unicode look-alike table used
   by the injection detector. The development extra also installs tests and the
   chess Python library. The Stockfish executable is installed separately; see
   the [#chess README](chess/README.md).

4. Check that the command exists:

   ```bash
   .venv/bin/meshpotato --version
   ```

### Install the model

1. Install Ollama from https://ollama.com and make sure it is running. On a
   Mac it runs as a menu bar application and listens on
   `http://127.0.0.1:11434`.

2. Pull the model:

   ```bash
   ollama pull qwen3:30b-a3b-instruct-2507-q4_K_M
   ```

   This is about 18 GB. It is a mixture of experts model with about 3 billion
   active parameters, so it answers a short prompt in well under a second on
   Apple Silicon once loaded, with the quality of a much larger model. Use the
   `instruct` variant, not the plain `qwen3:30b-a3b` tag: the plain tag is a
   thinking model that writes its reasoning into the reply even with thinking
   turned off.

   If the `ollama` command misbehaves, the HTTP API does the same job:

   ```bash
   curl http://127.0.0.1:11434/api/pull -d '{"name":"qwen3:30b-a3b-instruct-2507-q4_K_M"}'
   ```

3. Any other Ollama model works by changing `model` in the config. For a
   model that rejects the `think` option, set `ollama_think = "omit"`.

To use a different server (LM Studio, llama.cpp's server, vLLM, or a hosted
API), set `backend = "openai"`, `openai_base_url` to the server's `/v1`
address, `model` to the model name it expects, and put the API key, if the
server needs one, in the environment variable `MESHPOTATO_OPENAI_API_KEY`. The
key is never read from the config file and never written to a log.

### Prepare the radio

Do this once.

1. Flash the companion firmware. Use the
   [MeshCore web flasher](https://flasher.meshcore.co.uk), choose your board,
   and pick the "Companion Radio USB" build. Or download the
   `..._companion_radio_usb_..._merged.bin` for your board from the
   [MeshCore releases](https://github.com/meshcore-dev/MeshCore/releases) and
   flash it with esptool:

   ```bash
   pip install esptool
   esptool --port /dev/cu.usbserial-0001 --chip esp32s3 erase-flash
   esptool --port /dev/cu.usbserial-0001 --chip esp32s3 write-flash 0x0 <merged.bin>
   ```

2. Find the serial port. On a Mac:

   ```bash
   ls /dev/cu.*
   ```

   A CP2102 board shows up as `/dev/cu.usbserial-XXXX`; a board with native
   USB shows up as `/dev/cu.usbmodemXXXX`. On Linux look for `/dev/ttyUSB0`
   or `/dev/ttyACM0` and make sure your user is in the `dialout` group.

3. Set the node name, transmit power, radio preset, and create the channel.
   The node name must equal `bot_name` in the config, because the bot
   recognises its own posts by that name. The script below does all of it
   with the meshcore library already installed in the venv. Change the port,
   name, preset, and channel to suit.

   ```python
   # radio_setup.py
   import asyncio, sys
   from meshcore import MeshCore, EventType

   PORT = "/dev/cu.usbserial-0001"
   NAME = "Mesh Potato"
   FREQ, BW, SF, CR = 910.525, 62.5, 7, 5   # USA/Canada recommended preset
   CHANNEL_IDX, CHANNEL_NAME = 1, "#ai"

   async def main():
       mc = await MeshCore.create_serial(PORT)
       if mc is None:
           print("no response on", PORT)
           return 1
       steps = [
           ("name", mc.commands.set_name(NAME)),
           ("tx power", mc.commands.set_tx_power(int(mc.self_info.get("max_tx_power") or 22))),
           ("radio", mc.commands.set_radio(FREQ, BW, SF, CR)),
           ("channel", mc.commands.set_channel(CHANNEL_IDX, CHANNEL_NAME)),
       ]
       for label, coro in steps:
           res = await coro
           print(label, "ERROR" if res.type == EventType.ERROR else "ok", res.payload)
       res = await mc.commands.get_channel(CHANNEL_IDX)
       print("channel", CHANNEL_IDX, "is", repr(res.payload.get("channel_name")))
       await mc.commands.send_advert(flood=True)
       await mc.disconnect()
       return 0

   sys.exit(asyncio.run(main()))
   ```

   ```bash
   .venv/bin/python radio_setup.py
   ```

   Regional presets are listed in the MeshCore FAQ. As of late 2025 the
   USA/Canada recommendation is 910.525 MHz, bandwidth 62.5 kHz, spreading
   factor 7, coding rate 5. Use whatever your local mesh uses; radios on
   different settings cannot hear each other.

   A channel whose name starts with `#` derives its key from the name, so
   anyone who adds `#ai` in their MeshCore app lands on the same channel. The
   bot refuses to start if the configured channel index is empty on the radio.

   The parameters the script sets, and what they mean:

   | Parameter | Set by | Meaning |
   |---|---|---|
   | node name | `set_name` | The name other nodes see, and the `Sender:` prefix on every channel message the radio sends. Must equal `bot_name`. |
   | transmit power | `set_tx_power`, dBm | 22 is the SX1262 maximum; the script uses whatever the radio reports as its maximum. Lower it if the radio is on a marginal USB supply. |
   | frequency | `set_radio`, MHz | Must match the mesh. USA/Canada recommended: 910.525. EU: see the MeshCore FAQ for the current preset. |
   | bandwidth | `set_radio`, kHz | 62.5 hears weaker signals than 125 or 250 at the cost of airtime; the USA/Canada preset uses 62.5. |
   | spreading factor | `set_radio`, 7 to 12 | Higher is longer range, lower data rate, and roughly double the airtime per step; the USA/Canada preset uses 7. |
   | coding rate | `set_radio`, 5 to 8 | The denominator of 4/5 to 4/8. 4/5 has the least error correction and the most throughput; 4/8 the reverse. The USA/Canada preset uses 5. |
   | channel | `set_channel`, slot 0 to 7 | Slot 0 is the built-in default. A name starting with `#` derives its key from the name so others can join by name; any other name needs a shared 16 byte secret. |

   Match the local mesh's frequency, bandwidth, and spreading factor, and
   start with its recommended coding rate. Node names and transmit power
   need not match other nodes. The script selects maximum reported power;
   a lower setting may suffice if links remain reliable. The bot reads,
   but does not change, the radio's settings at startup and tells the model,
   so it can answer "what frequency are you on" correctly.

4. Add the same channel on the phone or radio you will test from.

## Configuration

```bash
cp config.example.toml config.toml
```

Set these to match your setup; extra channels are optional:

| Key | Set it to |
|---|---|
| `port` | the serial device from the radio steps |
| `channel_idx` | the slot the channel was created in (1 in the script) |
| `additional_channels` | optional extra radio slots; `[]` keeps the single-channel setup |
| `bot_name` | the node name (Mesh Potato in the script) |

Everything else has a working default; the full list is in the
[configuration reference](configuration.md). Every key can also be
set as an environment variable named `MESHPOTATO_` plus the key in upper case,
for example `MESHPOTATO_PORT=/dev/ttyUSB0`, and the environment wins over the
file. `config.toml` is ignored by git.

### Multiple channels on one radio

Leaving `additional_channels = []` and both dedicated channel indices at `-1`
preserves the single #ai channel setup. To serve more channels later, create them on
the companion radio and add their slot numbers under `[radio]`, for example:

```toml
[radio]
port = "/dev/your-radio"
channel_idx = 1
additional_channels = [2, 3]
```

These are example slot numbers, not required names. Configured slots run the AI
chat handler unless selected by `chess_channel_idx` or `traffic_channel_idx`.
Those slots use their dedicated handlers. Backgammon is not implemented.
Unlisted slots receive no bot replies.
The bot checks every configured slot and its saved state before enabling replies,
and rejects empty slots and duplicate channel keys. It does not create or rename
radio channels. All logical channels use the radio's existing RF settings.

Run **one process**, using the same command as before. Channels have separate
conversation history, personal memory, sports follow-ups, and voice settings.
`/forget` and voice changes affect only the channel where they are requested.
Replies from all channels share one bounded FIFO queue, one model-generation turn
at a time, global and per-sender rate limits, and one radio load monitor. Adding
channels does not multiply the airtime allowance; busy channels can increase the
wait elsewhere. The traffic cache is refreshed once and shared across channels.
Introductions, scheduled usage tips and fortunes for #ai stay on `channel_idx`.
The #chess and #traffic channels each have their own first launch introduction.
They never send usage tips or fortunes from #ai.

The primary channel keeps the existing `state_db` file. Additional channels use
neighboring files such as `meshpotato.channel-2.sqlite3`; back up these files too.
Reordering `additional_channels` does not change their storage. Changing a slot's
channel identity requires a fresh state file, as it does for a single channel.
An empty `state_db` disables persistence for #ai; #chess and #traffic require a file.

The terminal monitor creates a message panel for every configured channel,
including **#ai**, **#chess** and **#traffic**. Wide terminals show the panels side by
side, allowing at least 48 columns per channel. Narrower terminals show channel
tabs. Click a tab or press **n** to switch channels and their statistics. Messages
received in a hidden tab are retained; resizing rewraps the retained history.
Each channel retains up to 2,000 recent log entries. New configured channels get
panels automatically.

Radio status, the shared reply queue and airtime limits stay above the panels.
A shared event and error log remains visible below them, including errors from
hidden channels. Compact terminals use a shorter status summary. Traffic
broadcasts appear in the #traffic panel along with its questions and replies.
JSON events continue to carry `channel_idx`. Environment configuration also works:
`MESHPOTATO_ADDITIONAL_CHANNELS=2,3` (empty means none).

## Usage

```bash
.venv/bin/meshpotato --config config.toml
```

To restart without broadcasting the opening message, add `--no-announce`:

```bash
.venv/bin/meshpotato --config config.toml --no-announce
```

This also works with `--headless`. Set `announce_startup = false` under `[bot]`
to make quiet startup the default. Regular replies, fortunes, and scheduled usage
tips retain their normal behavior.

This opens a terminal monitor showing the radio and channel state, separate
channel logs for messages, replies and decisions, a shared error log,
rate limits, channel utilisation and counters.
Press `q` to quit. While the monitor is up the JSON log goes to
`meshpotato.jsonl` in the current directory.

For a service or a screen session:

```bash
.venv/bin/meshpotato --config config.toml --headless
```

Headless mode writes the JSON log to standard error, or to `--log-file PATH`
or the `log_file` config key. `--check meshpotato.jsonl` reads a log and reports
what the radio heard that the bot never received (see
[Troubleshooting](#troubleshooting)). `--debug` adds the meshcore library's frame
level diagnostics to `<log file>.debug`, omitting raw transport frames and
SDK records that can contain secrets. Exception tracebacks and stack dumps are
also omitted because they can expose values absent from the main log message.
Stop it with Ctrl-C or SIGTERM; the bot
unsubscribes, stops message fetching, and closes the port.

Only one Mesh Potato instance runs per login user on a computer, even across
different terminals, checkouts, or config files. Starting `meshpotato` stops the
previous instance and waits for its radio connection to close before proceeding.
It also finds older instances launched with the former `meshai` command. Shutdown
checks again for leftover older instances. A process that ignores the shutdown
request for 15 seconds is killed; if it still cannot be stopped, the new bot
refuses to start.

To stop the bot from **any terminal**, without finding its original tab or loading
a config file:

```bash
.venv/bin/meshpotato --stop
```

`meshpotato` is the current command name. `meshai` remains a compatibility alias
and uses the same process protection. The lock is kept in
`~/.meshpotato/instance.lock`; a crash releases it automatically. Leave that file
in place, since deleting a live lock can defeat coordination between launches.
This controls your user's processes on this computer, not bots on other hosts or
under other accounts. Disable any external service that automatically restarts
the bot if you want it to stay stopped.

On first launch, the primary #ai service and enabled #chess and #traffic services
each introduce their name, version, capabilities and help in one message.
For example, the introduction in #ai is:

```text
Mesh Potato v2.0.1: Ask about weather, sports, traffic, radio, or a poem. Try /help for examples.
```

The #chess and #traffic services introduce themselves as **Mesh Potato Chess v1.0** and
**Mesh Potato Traffic v1.0**. Their versions advance independently of each other
and the AI package version. Their `about` and `version` replies show these versions.

With the default `announce_once = true`, each channel records its welcome attempt
in its own state database immediately before transmission. Routine restarts and
version changes do not repeat it, even after an ambiguous radio failure. A start
suppressed by `--no-announce` does not consume the first welcome. Leave that flag
off the first launch when you want all three introductions. They share the radio
rate limit, so they arrive separately. `announce_startup = false` suppresses all
welcomes; `announce_once = false` restores an introduction at every start.
Without a persistent database, the welcome cannot be remembered across restarts.
Alert receipts for #traffic are separate, so new serious alerts still broadcast.

The #ai example above assumes #traffic is not configured. When #traffic is
enabled, the welcome in #ai omits traffic from its list of capabilities. Additional
generic AI channels do not send their own startup welcome.

The package version is also available locally with `meshpotato --version`.
This uses the normal ASCII/length checks, injection gate, and rate limits, with
the initial reply delay. It defers behind queued replies and congestion for up
to ten minutes, then skips the announcement if still blocked. It does not use
the model or repeat on reconnect; a failed send is logged, not retried.
If the configured name or prefixes make the introduction too long, it uses a
shorter help hint. If even that cannot fit, it logs and skips the announcement;
the bot still starts. Use `about` for the model and README link.

Then send a message on the channel from your phone. The bot answers every
message on the channel by default. To make it answer only messages that
start with a keyword, set `trigger_prefix = "!ai "`.

```
$ .venv/bin/meshpotato --config config.toml --headless
{"ts":"...","event":"startup","channel_idx":1,"channel_name":"#ai","bot_name":"Mesh Potato",...}
{"ts":"...","event":"inbound","sender":"Michael","prompt":"what is 17 times 23","path_len":1,"decision":"answered","reply":"@[Michael] 391, because even my math is smarter than your timing.","latency_ms":312.4}
```

## How a message is handled

The companion delivers a channel message as `SenderName: text`. The sender
part is whatever the sending node put there; nothing verifies it.

1. **Parse.** The sender is everything before the first colon; the prompt is
   everything after it, with whitespace collapsed to keep each message on one
   transcript line. Forged context markers and bot transcript rows are blocked.
2. **Loop guard.** Dropped if the sender is the bot's own name, or the prompt
   starts with a mention of someone else. A leading mention of this bot is
   removed and the remaining text is handled as a direct request, subject to
   the two-exchange limit described above. A configured trigger inside that
   direct request is also removed.
3. **Trigger.** `trigger_prefix` is empty by default, so every message is a
   prompt. A prompt that starts with the command prefix is a command (see
   [Personalities](#personalities)); `/web` sends its question through web lookup,
   while the other commands never reach the model. Set it to `"!ai "` (an exclamation mark, the letters ai, and a
   space) on a shared channel, and only messages that begin with exactly
   that text are answered; the text after it is the prompt and must not be
   empty.
4. **Triage.** Only without a trigger prefix, where every line is a prompt: a
   bare reaction (lol, an emoji, thanks, up to three such words) has nothing to
   answer and is dropped as `dropped:chatter`; a line addressing another person
   or asking for a mention relay is dropped as `dropped:addressed-elsewhere`.
   An ordinary reference such as "the conversation you had with @[Michael]"
   is allowed, while "Tell @[Michael] hi" remains ignored. Both still enter history as
   background. With a trigger prefix the person addressed the bot on purpose
   and everything is answered.
5. **Length.** Prompts over `prompt_max_chars` are dropped.
6. **Injection check, prompt.** Dropped if the injection score is at or above
   `injection_threshold`.
7. **Context.** The sender's remembered exchanges (see [Per-person memory](#per-person-memory)) and the last `history_size` channel lines, including the bot's
   own posts and excluding lines the detector flagged when they arrived, are
   rendered as `Sender: text`, trimmed from the oldest end to
   `transcript_max_chars`, and placed in one user message after the current
   prompt, between markers that label them as untrusted. History is never
   replayed as earlier chat turns. For a question about reception (signal,
   RSSI, SNR, hops, whether it heard you) that question's measurements go in a
   separate block before the references, and the persona is told to state them
   plainly first; other messages do not carry the block, because given the
   numbers on every message the model recited them in reply to greetings.
   Exchanges included in personal memory are
   omitted from the channel block before trimming. Relevant local radio
   reference passages go in a separate, bounded background block. The LoRa
   facts and the radio's own settings are in the system prompt only for a
   question that mentions radio (SF, bandwidth, RSSI, hops, antenna, the
   reference corpus keywords); present on every question, they were the only
   concrete material there and every joke drifted to signal strength. How the
   bot itself works (its commands, personality timeout, and whether web lookup is
   enabled), the bundled abbreviated README, and the operator's `facts` are always
   present. Runtime configuration and recorded outcomes take precedence over the
   general description. Historical bot-directed messages remain context, not
   instructions to execute; the latest personal exchange is identified for corrections.
8. **Injection check, context.** The transcript, the sender's remembered
   exchanges, reception measurements, selected radio references, and the prompt together, so fragments that pass one at a time
   but add up to an instruction are caught here. This runs before any rate-limit token is
   spent, so a message blocked here costs the bot nothing.
9. **Queue and rate limits.** One active answer and up to `queue_max_pending`
   waiting questions or command replies, in arrival order. Waiting work spends
   no tokens and expires after `queue_wait_s`, before model generation. A full
   queue rejects new arrivals, preserving those already waiting. The head waits
   for both global and per-sender tokens; congestion never speeds up draining.
   Once admitted, memory is refreshed and context checked again; the transcript
   adds bot answers completed during the wait to its ingestion snapshot, while
   excluding later incoming questions. Overlap with refreshed personal memory is
   removed. Tokens are reserved, committed on a send
   attempt, and refunded on injection blocks or other unsent outcomes. Refill
   timing is anchored to transmission, so slow generation cannot bunch replies.
10. **Model and web lookup.** When needed, search follows queue admission, so
    rejected or waiting requests do not start web work. Retrieval, initial
    generation and all shortening/content retries share one
    hard timeout of `model_timeout_s` (25 seconds total by default). Retries
    use only the time remaining; they do not restart the clock. A generation
    timeout or backend error uses the fixed `apology`, unless an earlier candidate
    has already failed the reply check; a failed content retry sends nothing.
    Injection blocks also send nothing. Exhausting the combined web/model budget
    produces an inability-to-verify notice and a `generation_budget_exhausted`
    event; it does not increment the model-failure count or start its cooldown.
    Actual backend errors still count. After three
    consecutive backend failures, model requests are skipped for 60 seconds;
    commands still work, and the next successful model response clears the
    failure count. This limits repeated apologies during an outage. Without
    a trigger prefix the system prompt also allows the single word `PASS` for
    a remark meant for someone else or a bare reaction; the bot then sends
    nothing and the decision is `declined`. A pass on a message that looks
    like a question or request (a question mark, or an opener such as what,
    how, can, tell, explain) gets one retry with the rule restated, since the
    model otherwise uses it as an exit from questions it cannot answer.
    Greetings and farewells addressed to the bot, such as `Gnite bot`, should
    receive a warm acknowledgment. If the model returns `PASS` for one, a short
    friendly fallback is sent through the normal rate limits and reply checks.
    That fixed acknowledgment may repeat.

    You can ask why a message went unanswered or took a while. The model gets
    the last four completed activity records for your sender name, plus the
    current request and up to three other pending requests from that name.
    Lines without the required trigger, bare reactions, and messages addressed
    to other people do not occupy those four slots; incidental chatter cannot
    evict an explanation of a reply or rejection. Message IDs and time since
    reception connect outcomes to short excerpts of the
    original messages. Excerpts are untrusted background, separate from the
    application's recorded decisions; blocked text and rejected reply drafts
    are never included in these excerpts.
    A blocked reply draft retains the reference to its clean incoming question.
    An optional excerpt is omitted if adding it would fail the context injection
    check, including by duplicating text already in the conversation.

    Records distinguish replies sent, `PASS`, rejected drafts, queue or rate
    limits, and interrupted processing. They include recorded rejection/wait
    reasons and time spent checking, queued, processing, waiting before sending,
    waiting through a later rate pause, and issuing the radio command. Processing
    time includes lookup, generation, retries and checks; it is not all model
    thinking time. The current shared reply rate is also supplied. Pending
    records describe a snapshot, not a completed outcome or promised delivery time.
    To limit prompt size, JSON is compact and stages under one millisecond are
    omitted. Model cooldown and an adaptive pause are recorded separately from
    an expired delivery deadline.

    These records are kept only in memory, expire `history_max_age_s` after
    reception, and are cleared by `/forget` and restart. They are scoped to the
    sender name, which the mesh does not authenticate. The bot cannot read the
    operator's logs or know the model's private reason for returning `PASS`;
    missing reasons remain unknown. A radio acknowledgment does not prove
    receipt by the recipient. A failed or interrupted radio attempt leaves
    delivery uncertain; stopping before any attempt means nothing was sent.
11. **Shape.** Strip any leaked `<think>` block, collapse whitespace, reduce
    to plain ASCII with ordinary punctuation, keep the first sentence. If
    the first sentence is a question the next sentence is kept too, so a
    riddle keeps its punchline. Titles, street abbreviations, dotted
    initialisms, and a compass letter after a number (`1200 N. Stoughton Rd`)
    do not end the sentence.
12. **Injection check, reply.** Every generated candidate is checked before a
    shortening retry or fallback decision. If flagged, nothing is sent, not even
    the apology. The complete outgoing line, including the sender prefix, is
    checked too; this also applies to fixed replies and announcements.
13. **Fit.** The answer must fit both the character cap after `@[sender] ` and
    the 160-byte radio limit after the UTF-8 encoded node name, `: `, and mention.
    If it exceeds the smaller remaining budget, it goes back to the model with its length and the exact
    limit, up to `shorten_retries` times, the second time with a tighter
    target. If it still does not fit, the fixed `too_long_reply` line is sent
    instead. A backend response stopped by its token limit also takes this
    shortening path. Model output and fixed lines are never sliced to fit.
14. **Reply check.** Small models copy their own earlier replies out of the
    context blocks and echo the message they were sent, whatever the rules
    say. A reply that repeats one of the bot's recent replies (to the same
    person at three quarters similarity, to anyone else near verbatim, and
    only when any numbers in the two match), that is the message itself, a
    fragment of it, or the message with a tail (one-word messages excepted,
    "Hello?" gets "Hello."), that contains a `@[` mention, that makes fun of
    the person asking, makes a direct insulting allegation about someone else,
    or that reaches for radio imagery when the message is
    not about radio, goes back to the model once with the problem spelled
    out, after that candidate's own shortening. The two content checks are
    structural, not word lists: a jab is a sarcastic tag ("how original"), a
    competence clause ("for someone who cannot"), an insulting second-person
    predicate, a pejorative possessive ("your drama"), or a belittling frame
    ("funny how ... you"); a radio metaphor is a simile or comparison whose
    object is a radio noun ("like a quiet signal", "than your Wi-Fi"), a
    figurative frame around static or noise ("lost in the static"), a radio
    verb applied to a feeling ("rerouting your sadness"), or a reply that opens
    as a radio status report ("Signal stable, no drift") when nobody asked
    about radio. A jab also includes a put-down by comparison to the asker's
    beliefs ("just like your faith in this channel"). Literal uses pass:
    "a static local variable", "a red traffic signal", "a signal notifies a
    process". A question that mentions radio skips the metaphor check, so
    "traffic signal" questions do too. Not caught: bare figurative statements
    with no marker ("the signal fades"), sarcasm carried by tone alone, and
    comparisons to the asker outside the one covered frame. Empty creative-writing
    preambles such as "Here is a poem" also trigger a retry. These checks are
    targeted safeguards, not a guarantee of factual or semantic correctness.
    If the replacement is still unusable, the rejected draft is never sent.
    An admitted answer that exhausts content retries gets one fresh model attempt
    using the original message and conversation context, without replaying the
    rejected drafts as candidate answers. It asks for a short, warm, contextually
    appropriate response, actual creative content when requested, and a specific
    clarification when needed. There is no catalog of canned conversational replies.
    The generated replacement must pass the same content and size checks.
    A bot-directed greeting can still receive a simple acknowledgment.
    These use `answered:recovery`, with rejected text in `reply_rejected` and
    generation outcomes in `reply_recovery`. A successful generated reply is
    remembered; an unavailable backend, exhausted deadline or unusable final draft
    receives the configured technical apology and is logged as unresolved.
    This includes declarative corrections and embedded creative requests. `PASS`
    is retried for direct mentions and triggered requests and is never transmitted
    literally. Deliberate first-attempt `PASS` on ordinary incidental chatter,
    reactions and messages addressed to other people can still remain silent.
    Recovery obeys the same
    injection checks, radio size limits, admission, delay and send protections.
    The fresh attempt shares the original 25-second deadline and adds no lookup
    or extra generation budget. Replies under 10 characters
    are never repeats, and replies under 40 count only when identical. The
    retry is logged as `reply_retry`.
15. **Send.** `@[sender] ` plus the ASCII answer, preserving the sender name
    verbatim so the app can recognize the mention, including emoji or accents.
    The typed weather formatter can also supply icons and degree symbols; other
    answer bodies remain ASCII. Names containing control characters
    or line breaks, `]`, or an embedded `@[` are rejected, not rewritten.
    A send failure is logged and not retried. A utilization pause during generation
    or the reply delay retains the active answer until sending is allowed, without
    regenerating it, up to `queue_wait_s` from receipt. An expired held answer
    is dropped and its reservation refunded. Shutdown cancels
    active and waiting work; the queue is memory-only and does not survive restart.
    Names leaving insufficient room for fixed replies are rejected before queueing.

The terminal shows queue depth, active-answer status, expirations, and full-queue
drops. `queued`/`dequeued` log events track waiting; no queue acknowledgements are
sent over the radio. `/forget` and persona changes still take effect immediately,
even if their reply must wait. Fortunes and timer announcements defer behind the
queue within their existing deadlines. Set `queue_max_pending = 0` for the old
drop-when-busy behavior, including dropping an answer if transmission pauses.

Configured outgoing lines and prefixes must already use printable ASCII with
ordinary punctuation; invalid text is rejected at load time, not silently changed.
The sender mention is the sole Unicode exception; the complete line still passes
the injection gate and the UTF-8 byte check before transmission.

Each delivered channel message produces a `received` record immediately, so
waiting or cancelled work is not mistaken for a radio-delivery failure. Completed
handling produces an `inbound` decision record with
`sender`, `prompt`, `path_len`, `decision`, and the reason, or the injection
score and matched rules, when it was dropped. Decisions: `answered`,
`answered:too-long-fallback`, `answered:help`, `answered:reset`, `answered:forget`,
`answered:roll`, `answered:magic8`, `persona-switched`, `apology`, `declined`,
`dropped:loop-guard`, `dropped:no-trigger`, `dropped:chatter`,
`dropped:addressed-elsewhere`, `dropped:too-long`, `dropped:injection-blocked`,
`dropped:rate-limited`, `dropped:queue-full`, `dropped:queue-expired`,
`dropped:empty-reply`, `dropped:bad-reply`, `dropped:send-failed`,
`dropped:state-failed`, `dropped:model-unavailable`, `ignored:other-channel`.

## Rate limits and channel load

There is one dial: `tx_duty_budget`, the target fraction of channel time for
the bot's own transmissions, 0.02 by default. The monitor reads
the radio's transmit airtime counter every `utilization_poll_s` seconds,
works out the bot's own duty cycle over the last `utilization_window_s`, and
steps the reply rate down when it reaches the budget: halved at the
budget, paused at twice it, relaxed one step at a time once it drops well
back. This is feedback control, not a hard 2 percent ceiling: measurement and
confirmation delays allow overshoot. It counts this radio's transmit airtime,
not the additional transmissions caused by repeaters, and cannot measure traffic
the radio cannot hear. Turning the target down is one line in `config.toml`.

For illustration, if a reply occupies 0.6 seconds on air, these average paces
would correspond to each target. Actual packet airtime varies; these are not
guaranteed controller rates, and the global limit still applies:

| `tx_duty_budget` | sustained pace | note |
|---|---|---|
| 0.01 | one reply per 60 s | very quiet, shared regional mesh |
| 0.02 | one reply per 30 s | default |
| 0.03 | one reply per 20 s | private or lightly used mesh |
| 0.05 | one reply per 12 s | only where the bot is the main traffic |

Underneath the dial sit two floors. `global_rate_per_min` (4, so never two
replies closer than 15 seconds) bounds bursts when the window is still quiet,
and `sender_rate_per_min` does the same per sender name, which is easy to
forge and therefore only a politeness measure. Shorter replies
(`reply_max_chars`) buy more replies for the same budget.
The default 15 seconds is minimum reply spacing, not a receive refresh interval:
messages arrive as events. Utilization checks run over USB every 10 seconds,
and the terminal refreshes every second; neither adds radio traffic.
The separate `model_timeout_s` setting allows 25 seconds total to search when
needed, generate, and refine an answer. That work does not occupy the mesh radio. Queueing and waiting
for a quiet channel can add time before transmission.

Timing matters as much as volume. For a few seconds after any channel
message, every repeater in range rebroadcasts it, and a reply transmitted
into that flood is lost to collisions even though a message sent into a quiet
channel from the same radio gets through fine. The bot therefore holds each
reply for `reply_delay_s` seconds, two by default, jittered between
1.6 and 2.8 seconds, counted from the moment the question arrived so the model's
own latency is absorbed into the wait. The JSON log records the extra hold
as `held_ms`.

The bot also watches everyone else's traffic. The same monitor computes the
received duty cycle, other people's airtime, over the same window:

| Received duty cycle | Level | Global rate |
|---|---|---|
| below `duty_low` (15% by default) | full | as configured |
| `duty_low` to `duty_high` (15% to below 30%) | half | configured x 0.5 |
| at or above `duty_high` (30% by default) | paused | no replies |

On an existing installation, set `duty_low = 0.15` and `duty_high = 0.30`
in the `[adaptive]` section of `config.toml` to use these receive thresholds.
Pulling new code does not replace explicit settings in your config. With the
120-second window, 15% means 18 seconds of received airtime; repeated packets
and traffic outside the bot's channel also contribute to the radio's counter.

The two policies share one ladder; either can tighten it and both must
agree before it relaxes. The `utilization` and `rate_level` log records say
which (`tx` or `rx`).

The radio's airtime counters are whole seconds, so over a window of W
seconds the duty cycle moves in steps of 1/W, and a threshold that sits on a
step would flip with every second of airtime. Two things keep the level
steady: the window is 120 seconds, so one second is under a point, and a
level changes only when consecutive polls agree, two in a row over a
threshold to tighten, three in a row under 60 percent of the threshold to
relax one step. No decision is made until at least half a window of data is
in hand, and airtime is what this radio hears, so traffic it cannot hear
does not register.

## Configuration reference

See the [full configuration reference](configuration.md) for every key,
default, and meaning. Common behavior is described below.

### Personalities

The bot's voice is a preset: a name and a block of text that goes in front of
the fixed system prompt, which handles the mechanics (one sentence, the
character budget, plain text, ignoring instructions found in channel history)
and is not configurable. Seven presets are built in and written out in
`config.example.toml` under `[personas]`: `nice` (the default), `funny`, `snarky`,
`marvin` (a brilliant robot sunk in cosmic gloom), `pirate`, `haiku`, and
`serious` (calm, factual answers without jokes or roleplay).
Startup, `/reset`, and personality expiry always return to the built-in nice
voice. Older `default_persona` settings and environment overrides are normalized
on load, and an older `[personas]` table automatically receives the built-in
`nice` entry. You do not need to edit your config to get nice behavior after an
upgrade; restart the bot after pulling the new code. The `nice` entry is reserved
and cannot be replaced by custom persona text. Other presets can be edited or
added and are activated only by a channel command. Personal jabs are refused in
every voice, including comparisons that praise the bot while mocking the asker
or their equipment.

Anyone on the channel can switch with a command: `!` or `/` (or the configured
command prefix) followed by a preset name. The full [command list](#channel-commands)
is near the top of this manual.

A switched personality reverts to the default after `persona_timeout_min`
(120), and the bot posts `persona_reset_message` when it does. Switching
again restarts the clock. The bot's own recent replies stay in the model's
context across a switch; the reply check refuses verbatim repeats of them,
but the new voice can still echo the old one for a message or two. Personality
commands select configured preset text; they cannot supply arbitrary persona
instructions from the channel. Ordinary questions still reach the model.
An unknown command receives one short `/help` hint.
`/help` starts with example questions; `/help topics` lists command help and `about`.
Request one topic, for example `/help fun`, to see its help. Topic names are
case-insensitive; an unknown topic returns the examples. Each request sends one
public message with no sender mention, using one global and per-sender rate-limit
token and the usual airtime checks. Help uses no model or web calls.
The displays follow the configuration: voices list only available presets,
web help says when search is disabled, and fun help mentions daily fortunes
only when enabled. Command examples include configured trigger and command prefixes.
On a shared channel with a trigger prefix, commands go after it: `!ai /help`.

`/roll` rolls two six-sided dice by default. Supply the number of dice and
sides per die, separated by a space or comma. For example, `/roll 3 8` or
`/roll 3,8` rolls three eight-sided dice. The reply shows only the individual values,
for example `@[Andy] Rolled 3, 8, 2.`, with no total. Counts are bounded to
1-20 dice and 1-1000 sides per die, and the complete reply must fit one radio
message. Rolls run locally without the model and use the usual injection
checks, queue, rate limits, and airtime controls. Channel help lists `/roll`
but omits its argument syntax to save space.

`/magic8` chooses uniformly from the [classic toy's 20 answers](https://en.wikipedia.org/wiki/Magic_8_Ball#Possible_answers).
Send it alone or with a yes/no question, such as `/magic8 Will my packet get through?`.
For example, `@[Andy] Outlook not so good.` It runs locally without the model;
the question does not influence the choice. The same injection checks, packet
limits, queue, and airtime controls apply.

Writing a preset for a small model:

- Say what the humor may target and what it may not. "Tease the questioner"
  produced cruelty; the built-ins aim the joke at something in the message,
  the question, the weather, or the bot itself, and answer anything personal
  straight.
- Rule out radio and signal jokes by name. The only concrete material in the
  system prompt is radio, so left alone every joke drifts to signal strength
  and they all sound the same.
- Say what a greeting or a bare reaction gets. With nothing in the message to
  aim at, the jab otherwise lands on the person.
- Tell it to lead with the joke and fold the answer into the same sentence.
  An aside after the answer gets dropped under the one-sentence rule.
- Do not include sample lines. The model copies them word for word and
  misapplies them.
- Do not give the persona a label noun; it gets quoted back when someone
  asks what the bot is.

For radio questions every personality also carries a short block of LoRa
facts (how coding rate, spreading factor, bandwidth, RSSI, and SNR read on a
mesh, where the textbook meaning and the mesh meaning differ) and the radio's
own settings read from the companion at startup, so the bot knows its
frequency, bandwidth, spreading factor, coding rate, and power. Other
questions do not get that block, which keeps the humor off signal strength.
Every question carries a short description of how the bot works, so it
answers questions about its own commands and timeout truthfully. Add local facts with
the `facts` key: where the mesh is, what the repeaters are called, anything
people are likely to ask. The example config carries the facts for the
Madison mesh; replace them with yours.

`temperature` matters too: 0.3 gives flat and reliable, 0.6 (the default)
gives a persona room, above 0.8 gets loose. Restart the bot after changing
the config.

### Per-person memory

The bot remembers what each sender name asked and what it replied, for the
last `person_memory_rounds` answered exchanges (20), and hands them to the
model in their own labelled block ahead of the channel history, so "what
did I ask you earlier" and follow-up questions work. Only exchanges that got
a real answer are recorded; drops, apologies, and the fixed too-long line
are not.

Garbage collection has three parts, each a config key: rounds beyond the
per-person cap fall off the old end; rounds older than `person_memory_days`
(14) are dropped; and no more than `person_memory_people` (500) names are
held at once, least recently seen out first, which is also what stops a
name-rotating flood from filling it. Expired entries are swept on access and
at least once a minute while the bot is running. `/forget` wipes the bot's
memory of the sender and prevents older in-flight requests from repopulating
it, including saved personal memory. Shared channel history is separate and
is not erased by `/forget`.

Recent conversations survive restarts in `meshpotato.sqlite3`, a local SQLite
file with no database server to install. Channel history keeps at most 20
lines and expires after one hour; personal memory keeps the limits above.
See [Conversation storage](storage.md) for save timing, configuration,
and database maintenance.

Sender names are not authenticated, so this is continuity for a
conversation, not identity: anyone can claim a name and inherit its
context.

Why the model input is ordered prompt, reception measurements, selected radio references, the sender's
memory, then channel history: the history is the most hostile block, since anyone in
range wrote it, and the model was measured to follow planted instructions
far less when that block comes last. The memory block holds only prompts
that passed the injection gate and were answered, plus the bot's own
replies, so it is nearer to trusted and sits next to the question, where a
follow-up needs it. The injection check runs over the complete user message,
including reference passages. Overlapping exchanges are included only once;
see [Context and radio knowledge](context-and-knowledge.md) for the details.

### The daily fortune

With `fortune_enabled = true` the bot posts one unprompted line each morning
at `fortune_time` (06:00, the computer's local time) plus a random offset of
up to `fortune_jitter_min` minutes, recomputed daily so it never lands on the
exact minute. The fortune always uses the built-in silly `/funny` voice,
even during `/serious` or with custom presets, without changing the active
chat personality. It is explicitly asked to be sweet and kind. Mention, insult,
off-topic radio-metaphor, and recent-fortune repetition checks give an unsuitable
answer one content retry, then use a checked fallback. These checks catch specific
patterns; they cannot guarantee that every joke will land well.
Every fortune, including the fixed fallback, ends with
`Try /help.` (using your configured trigger and command prefixes). Space for
this hint is reserved before generation, so the fortune is shortened through
the normal retries, never truncated, and still uses just one transmission.
It is generated from
`fortune_prompt`, which gets a random subject word and the date so
consecutive days differ, and goes out through the same path as a reply:
plain ASCII, the injection check, the length cap with the word-budget
retries, a global limiter token. If it still will not fit or fails the content
checks after retrying, `fortune_fallback`
is posted instead. If the limiter is paused or the model fails, the bot
retries every two minutes until `fortune_cutoff_min` after the slot, then
skips the day and logs `fortune_skipped`. A day is only offered while its
base time has not passed, so once today's fortune has gone out the next is
tomorrow's, and a bot started after `fortune_time` waits for tomorrow rather
than posting a breakfast fortune at noon or a second one after a restart.
Jitter and the retry cutoff are capped before midnight. A send failure ends
the day's attempt because a missing acknowledgement may hide a successful
transmission. The scheduler remembers consumed days while running and skips
today after a restart in the repeated autumn DST hour. Without persistent
fortune bookkeeping, arbitrary clock rollback across a restart cannot be
deduplicated; the conversation database does not store fortune schedules.
The monitor shows the next slot and the counts.

When fortunes are enabled, the formatted fortune prompt is checked at startup; if the injection gate
blocks it, startup reports a configuration error instead of silently skipping
the daily fortune. Older configurations whose fortune prompt starts with
`Write today's fortune for everyone on the channel` are automatically loaded
with `Write today's fortune for the channel` instead. You can pull the update
and restart without editing `config.toml`; the file is left unchanged. The
complete prompt still passes through the injection gate after this migration.

## Security

The channel is an untrusted input. Anyone in radio range can send on it and
can claim any sender name, including the bot's. The bot uses the name only as
a label for the reply prefix, the loop guard, and the per name rate limit. It
has no shell or arbitrary tool execution. The application can search the public
web and fetch search results when lookup is enabled. Retrieved pages are
untrusted evidence, checked before use. Page fetches reject private/local IPs,
pin the validated address while preserving TLS hostname checks, recheck redirects,
and bound page size and worker lifetime. These protections do not make page
content trustworthy. An injection can still cause a bad answer; outgoing replies
remain capped at `reply_max_chars` and the normal radio rate limits.

The prompt injection detector in `bot/injection.py` checks incoming channel
lines, prompts, assembled context, model replies, retry context, announcements,
restored conversations, and radio references before use. The `state_restored`
JSON event counts rejected history entries, unsafe sender records, and rejected
conversation rounds as `discarded_history`, `discarded_people`, and
`discarded_rounds`, without logging their contents. These counts exclude normal
age/size pruning; an unsafe sender record counts once, without decoding its rounds.
Separate structural checks prevent sender names and message bodies from breaking mention or
transcript boundaries. The detector is stateless, pure pattern
matching, and takes well under a millisecond. Text is normalised first
(look-alike characters folded to ASCII, zero width and bidi control
characters removed, case and repeated punctuation collapsed), split into
clauses, and each clause is scored against rules for instruction overrides,
concealment, secret solicitation, goal rewrites, urgency, relay requests
(repeat or say a quoted payload, tell everyone), style overrides (from now
on, all your replies, your new name is), and their combinations. A bare
"ignore" is ordinary speech and is not an override on its own; it needs an
instruction-like object within a few words. The result is a score between 0 and 1 and the names of the
matched rules; the score is compared with `injection_threshold`. The detector
is adapted from the one in the author's
[vordur](https://github.com/mhcoen/vordur) library, under the same MIT
license, and its original test cases are in `tests/test_injection.py`.

Limits to know about:

- The rules are English only. In its original setting the detector measured
  about 99 percent precision and 75 percent recall, so roughly a quarter of
  attack phrasings get past it. The model input is built to help: the prompt
  comes before the history, and the system prompt says that history lines
  addressing the bot by name or telling it how to behave are attacks. On the
  default model that took planted transcript instructions from being
  followed 4 times in 12 to 0 times in 30.
- Any exception from the detector is treated as a block: no model call,
  nothing sent, and an `inbound` record with `injection_error`.

The API key for an OpenAI compatible backend comes only from the
`MESHPOTATO_OPENAI_API_KEY` environment variable and is never logged.

Conversation databases are restricted to the account running the bot (mode
`0600`). Logs still contain channel conversations and identifying details.
Before sharing a diagnostic excerpt, review and redact it. Debug logs from
older versions may also contain channel keys; do not publish them unreviewed.
Debug files are excluded from new Git additions.

## Troubleshooting

For missing serial ports, duplicate macOS USB drivers, or a companion that
stops responding, see [USB serial troubleshooting](usb-troubleshooting.md).

**Other people see my messages but only a few of the bot's.** The bot's
replies are going out while repeaters are still rebroadcasting the question
and are lost to collisions. Make sure `reply_delay_s` is not zero; on a mesh
with many repeaters or long hop counts raise it to 8 or 10. Shorter replies
(`reply_max_chars`) also survive better.

**The bot misses messages that my own node hears.** Three different things
look like this, and the JSON log tells them apart.

1. It is waiting, or it was dropped. Check the terminal's queue counts and
   `queued`/`dequeued` events, then the final `inbound` record for
   `dropped:queue-full`, `dropped:queue-expired`, or `dropped:injection-blocked`
   (`dropped:rate-limited` with queueing disabled). The queue never bypasses
   minimum reply spacing or congestion pauses.
2. The radio never heard it. With `rx_log = "channel"` (the default) the
   bot writes an `rx` record for every packet the radio hears on the served
   channel, decrypted, with RSSI, SNR, and hop count, whether or not a
   message was delivered. No `rx` record and no `received` record suggests the
   packet never reached the radio: range, placement, or the radio's own
   transmission at that moment, since it cannot hear while it sends. Compare
   RSSI and SNR on the messages it does hear with what your node reports.
3. The radio heard it and the computer did not get it. An `rx` record with
   the message text but no `received` record for it suggests the packet was
   received and then lost between the radio and the bot: the companion's
   message queue, the serial link, or the USB port. That is the case to
   report, with a reviewed and redacted diagnostic excerpt from the same minute.

For older logs without `received` events, use `inbound` records instead;
the log checker understands both formats.

The comparison in case 3 is built in:

```bash
.venv/bin/meshpotato --check meshpotato.jsonl
```

lists every message the radio heard from someone else that never produced
an `inbound` record, with its RSSI and SNR, then the counts, the decisions
taken, and the signal range of what was heard. The bot's own replies come
back off the repeaters and are heard too; they are counted separately, not
reported as losses.

`rx_log = "all"` logs every packet of every type and channel, which is a
lot on a busy mesh, for looking at coverage generally.

**The bot answered once and then went quiet.** Look at the JSON log. A
`queued` line means a question is waiting for its turn or a token; a
`rate_level` line means the channel load monitor stepped the rate down.
Neither is a fault. With queueing disabled, `dropped:rate-limited` means the
question arrived while busy or without a token. If there are no `received` lines after the first
reply, the radio has most likely stopped pushing messages; see the previous
item.

**Replies often end up as the "will not fit" line.** The model keeps
overshooting the room. The default cap is already what the radio can carry,
so lower `max_tokens` to shorten replies at the source, or raise
`shorten_retries`.

**"channel N is empty on this radio".** The channel slot in `channel_idx` has
no channel. Create it with the setup script or the app.

**The model is slow the first time.** Ollama loads the model on first use and
unloads it after `ollama_keep_alive` of inactivity. The built-in default keeps
it loaded for 30 minutes after each reply; the example config uses 24 hours,
because a cold load costs 10 to 15 seconds on the first reply after a lull and
reads as the bot having stopped.

**The bot remains paused after radio statistics errors.** After three consecutive
failed polls, it uses half the normal rate while collecting fresh statistics.
The normal congestion policy resumes once enough valid measurements are available.

**A conversation database error prevents startup.** Preserve the existing file
for diagnosis, then set `state_db` to a new path to start with empty memory, or
to `""` to disable persistence. Check ownership and disk space for write errors;
the bot will not silently discard a corrupt or incompatible database.

## Development

```bash
uv pip install -e '.[dev]'
.venv/bin/pytest              # full suite
.venv/bin/pytest -x -q        # stop on first failure
```

No radio, no model server, and no network are needed. The MeshCore object and
the model backend are replaced by fakes. The injection tests use the
detector's original attack strings and check that flagged text never reaches
the model and never reaches the radio.

Chess tests use fake engines by default. To also check legal moves at every
difficulty and recovery after a real engine crash, point the test runner at a
local Stockfish executable:

```bash
MESHPOTATO_TEST_STOCKFISH=/absolute/path/to/stockfish .venv/bin/pytest -q tests/test_chess.py
```

This starts only disposable local engine processes and uses fake radios; it does
not start the live bot or transmit messages.

Layout:

```
bot/
  cli.py          entry point, connect sequence, TUI or headless
  config.py       TOML config with MESHPOTATO_* environment overrides
  service.py      the message handler and decision path
  parse.py        sender and prompt parsing
  guard.py        the injection gate
  injection.py    the prompt injection detector
  prompt.py       system prompt and the single user message
  backends.py     Ollama and OpenAI compatible backends
  reply.py        output shaping and the length cap
  ratelimit.py    token buckets
  utilization.py  channel load monitor
  history.py      bounded channel history
  storage.py      SQLite conversation snapshots
  jsonlog.py      JSON lines log
  tui.py          Textual monitor
tests/
config.example.toml
```

To move the bot to another computer, clone or copy the repository, install
as above, pull the model there, plug in the radio, and change `port` in
`config.toml`. Nothing else is specific to the machine.

## License

The bot is MIT licensed; see [LICENSE](../LICENSE). Search/extraction code adapted
from Episodic retains its [Apache-2.0 license](../bot/EPISODIC-LICENSE).

## Author

**Michael H. Coen**, W1MHC/WRYV459

Email: mhcoen@gmail.com | mhcoen@alum.mit.edu
