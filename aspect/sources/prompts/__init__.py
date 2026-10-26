"""Parsers for the chat `system` prompt, which is how a *hidden* definition is
recovered: the site sends the definition to the model, the proxy is the model, so
the definition arrives here in plaintext. One parser per site's prompt layout.
"""
