"""SSA construction and destruction mini toolchain.

Pure-backend educational toolkit:

* lexer/parser for a small imperative integer language (``ssa_toolchain.parser``)
* non-SSA integer IR with branches/loops (``ssa_toolchain.ir``)
* dominance analysis (``ssa_toolchain.dominance``)
* SSA construction with phi insertion + renaming (``ssa_toolchain.ssa_construct``)
* phi elimination with critical-edge splitting and parallel-copy
  cycle breaking (``ssa_toolchain.ssa_destroy``)
* interpreters to execute the original and lowered IR (``ssa_toolchain.interp``)
* JSON request service (``ssa_toolchain.service``)

Nothing here delegates parsing or SSA analysis to an external compiler.
"""

__version__ = "1.0.0"
