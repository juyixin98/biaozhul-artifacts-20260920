"""Regex-rule lexer generator backend.

Package layout (engineering boundaries):

- ``app.corpus``   — corpus specification: token-rule schema, fixed Unicode
  ranges and newline mode.
- ``app.kernel``   — mining kernel: regex parsing, automata, lexer generation,
  overlap / unreachability diagnostics.
- ``app.index``    — index & model: SQLite persistence of rule sets,
  diagnostics and run logs.
- ``app.query``    — query validation: request-boundary validation and limits.
"""
