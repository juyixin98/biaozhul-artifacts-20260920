"""TaintLang: a small-language toolchain for cross-function taint propagation.

Modules:
  location  - source spans (line/column offsets, 1-based)
  errors    - parse / build / analysis exceptions
  config    - analysis configuration (source/sink/sanitizer markers, context depth)
  lexer     - hand-written tokenizer
  ast_nodes - syntax tree node definitions
  parser    - hand-written recursive-descent parser
  ir        - custom instruction-level IR and CFG
  builder   - AST -> IR lowering
  analyzer  - context-bounded interprocedural dataflow analysis
  service   - zero-dependency JSON/HTTP service
  cli       - command line front end
"""

__version__ = "1.0.0"
