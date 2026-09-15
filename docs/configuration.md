# Configuration reference

[Back to the README](../README.md#configuration) | [Example configuration](../config.example.toml)

All keys with their defaults. Sections in the file are for readability only;
any key may appear in any section.

| Key | Default | Meaning |
|---|---|---|
| `port` | required | Serial device of the companion radio |
| `channel_idx` | `1` | Channel slot on the radio to serve |
| `bot_name` | `Mesh Potato` | Must equal the radio's node name |
| `trigger_prefix` | `""` | Off by default, so every message is a prompt, except bare reactions and lines mentioning someone else, and the model may pass on remarks between other people; `"!ai "` answers only messages beginning with that exact text |
| `reply_max_chars` | automatic | Omit to calculate 160 minus the UTF-8 byte length of `bot_name` minus 2 (147 for Mesh Potato). Optionally set a smaller character cap, including the exact-name mention; every reply is also checked in UTF-8 bytes. |
| `prompt_max_chars` | `160` | Longer prompts are dropped |
| `reply_delay_s` | `8.0` | Seconds after a question before the reply is transmitted, jittered; see [Rate limits and channel load](../README.md#rate-limits-and-channel-load) |
| `shorten_retries` | `2` | Times a reply that does not fit goes back to the model with the exact limit |
| `too_long_reply` | `That answer will not fit in one message, ask me something narrower.` | Sent when it still does not fit after the retries |
| `apology` | `Sorry, I couldn't answer that one.` | Posted on model timeout or error |
| `facts` | `""` | Local facts added to the system prompt after the built-in LoRa facts and the radio's own settings |
| `[personas]` | seven built-ins | Table of name = text presets, including nice and serious; explicit tables replace other built-ins but always receive the reserved built-in nice voice; see [Personalities](../README.md#personalities) |
| `default_persona` | `nice` | Compatibility setting: normalized to `nice` on load, including old configs and environment overrides; startup/reset/expiry always use the built-in nice voice, other voices require a command |
| `persona_timeout_min` | `120` | A switched personality reverts after this long |
| `persona_reset_message` | `Back to the default personality.` | Posted when it reverts |
| `command_prefix` | `/` | Commands are this prefix plus a preset name, `help`, or `reset` |
| `backend` | `ollama` | `ollama` or `openai` |
| `model` | `qwen3:30b-a3b-instruct-2507-q4_K_M` | Model name for the backend |
| `ollama_host` | `http://127.0.0.1:11434` | Ollama server |
| `ollama_think` | `off` | `off`, `on`, or `omit` for models that reject the option |
| `ollama_keep_alive` | `30m` | How long Ollama keeps the model loaded between replies; the example config uses `24h`, since a cold load costs 10 to 15 s on the first reply after a lull |
| `openai_base_url` | `http://127.0.0.1:1234/v1` | OpenAI compatible server, when `backend = "openai"` |
| `temperature` | `0.6` | Sampling temperature |
| `max_tokens` | `80` | Ordinary reply output token limit; the shipped backends allow 384 tokens for web JSON and its quote, with the same radio text cap; adapters without a token override retain their configured limit |
| `model_timeout_s` | `25.0` | One total deadline shared by web retrieval, initial generation and every shortening/content retry; retrieval uses at most 12 seconds of this budget |
| `web_enabled` | `true` | Automatic current-information lookup and `/web`; NFL/NBA/WNBA/MLB/NHL scores, active-season standings and next games use structured ESPN feeds without the model; sports pronoun follow-ups require that sender's recent successfully sent team answer; other searches send the question to DuckDuckGo and fetch up to three public result pages |
| `web_location` | `Madison, Wisconsin` | Default location for local weather/hours questions without an explicit location; dates, including sports game dates, use the computer's local timezone |
| `global_rate_per_min` | `4.0` | Burst floor: replies per minute across all senders |
| `global_burst` | `1` | Global bucket size |
| `sender_rate_per_min` | `4.0` | Replies per minute per sender name |
| `sender_burst` | `1` | Per sender bucket size |
| `queue_max_pending` | `10` | Waiting questions/command replies, excluding the active answer; 0 restores drop-when-busy |
| `queue_wait_s` | `600.0` | Maximum time from receipt to send, including a congestion pause after generation, when queueing is enabled |
| `fortune_enabled` | `true` | Post a daily fortune |
| `fortune_time` | `06:00` | Local time; a random offset up to `fortune_jitter_min` is added each day |
| `fortune_jitter_min` | `12` | Random offset after `fortune_time` |
| `fortune_cutoff_min` | `30` | Keep retrying a deferred fortune until this long after the slot, then skip the day |
| `fortune_prefix` | `Fortune: ` | Lead-in on the post |
| `fortune_prompt` | see example config | The request to the model; must contain `{subject}`, may use `{date}`; the legacy shipped opening "Write today's fortune for everyone on the channel" is migrated automatically in memory; a blocked formatted prompt is a startup configuration error when fortunes are enabled |
| `fortune_fallback` | `A small kindness will return wearing a tiny party hat.` | Checked fallback if generation is empty, oversized, or fails content checks after retries |
| `adaptive_enabled` | `true` | Scale the global rate by channel load |
| `utilization_poll_s` | `10.0` | Seconds between radio statistics polls |
| `utilization_window_s` | `120.0` | Window for the duty cycle |
| `duty_low` | `0.15` | Receive duty cycle at which the rate is halved |
| `duty_high` | `0.30` | Receive duty cycle at which replies pause |
| `tx_duty_budget` | `0.02` | Own-transmit airtime target, not a hard ceiling or network-wide budget |
| `state_db` | `meshpotato.sqlite3` | SQLite conversation file, restricted to its owner (0600), relative to the working directory; `""` disables persistence |
| `state_save_interval_s` | `5.0` | Seconds between snapshots; also saves on clean shutdown and immediately for `/forget` |
| `history_size` | `20` | Maximum recent channel lines, including saved history |
| `history_max_age_s` | `3600.0` | Expire channel lines after one hour, including across restarts; also bounds in-memory activity records by time since reception |
| `transcript_max_chars` | `1500` | Size of the transcript given to the model |
| `person_memory_rounds` | `20` | Answered exchanges remembered per sender name |
| `person_memory_days` | `14` | Rounds older than this are dropped |
| `person_memory_people` | `500` | Names remembered at once, least recently seen out first |
| `person_memory_max_chars` | `600` | Size of the remembered block given to the model |
| `injection_threshold` | `0.45` | Block a message whose injection score is at or above this |
| `rx_log` | `channel` | Log packets the radio hears: `off`, `channel` (the served channel), or `all` |
| `log_file` | `""` | JSON log path; empty means standard error (headless) or `meshpotato.jsonl` (monitor) |
