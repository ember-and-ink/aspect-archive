"""How the server presents itself while running: the live terminal dashboard it
draws instead of a scrolling log, and the canned responder that answers
`/v1/chat/completions`.

Neither is part of the archive. The responder exists because a reply is the side
effect of getting a hidden definition into a system prompt -- there is no model
behind it (see `mock_responder`).
"""
