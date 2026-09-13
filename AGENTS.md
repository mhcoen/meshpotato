# Working in this repository

- Within the user's requested work, fix confirmed defects you discover and verify
  the result. Do not stop at diagnosis or ask the user to repeat "do it."
- An explicit request for a read-only audit, discussion, or no implementation
  takes precedence. Otherwise, carry authorized repairs through to completion.
- Check audit recommendations against the code. Preserve intentional protections,
  especially single-instance ownership and avoiding duplicate radio transmissions
  after an ambiguous send failure.
- Use isolated tests with fake radios and model backends. Process tests must stub
  scans or restrict them to disposable children they created. Do not operate a
  live bot, deploy, or transmit over a radio unless the user authorizes that work.
- Keep the main README accurate for users when behavior changes. Preserve
  unrelated working-tree changes and report the actual validation performed.
- `model_timeout_s` is one total generation budget (25 seconds by default),
  shared by web retrieval, the initial call and every shortening/content retry. Do not reset
  it per call or increase the total in response to a reviewer's recommendation;
  changing this latency constraint requires the user's explicit instruction.
- Nice is the shipped default voice; funny and other personalities are by request.
  Fortunes remain funny and sweet regardless of the active chat personality.
- Web lookup is a production feature: automatic current-information routing plus
  `/web`, using the extracted Episodic search/extraction pieces. Keep its tests
  isolated and its user-facing documentation in the main README.
