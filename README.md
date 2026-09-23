# SSA 构造与回退（SSA construction & destruction）— pure backend

A from-scratch, dependency-free Python toolchain that compiles a small
integer language into a branch/loop IR, builds **static single-assignment
form** (dominance, phi insertion, renaming), then lowers SSA back to an
executable phi-free IR by splitting critical edges and sequencing
parallel copies (including copy cycles). An interpreter runs the original
IR and the lowered IR; the two results are the correctness oracle.

No parser, IR, dominator or SSA routine comes from an external compiler —
lexer, parser, CFG analyses and interpreters are all hand-written in this
repo (`ssa_toolchain/`). There is deliberately **no frontend product UI**.

## What is implemented

| Requirement | Where |
|---|---|
| Custom language, documented grammar | [`LANGUAGE.md`](LANGUAGE.md), `ssa_toolchain/parser.py` |
| Source positions preserved | `ssa_toolchain/errors.py` (`Loc`), carried on every AST node and IR instruction |
| Hand-written lexer / recursive-descent parser | `ssa_toolchain/lexer.py`, `ssa_toolchain/parser.py` |
| Integer IR with branches and loops | `ssa_toolchain/ir.py`, lowered in `ssa_toolchain/frontend/builder.py` |
| Reachability / unreachable block pruning | `ssa_toolchain/analysis/dominance.py` |
| Immediate dominators (CHK iterative algorithm over RPO) | `ssa_toolchain/analysis/dominance.py` |
| Dominator sets, dominator tree, dominance frontiers (Cytron) | `ssa_toolchain/analysis/dominance.py` |
| Phi insertion on iterated dominance frontiers | `ssa_toolchain/analysis/ssa_construct.py` |
| Dominator-tree preorder renaming (stacks, edge args) | `ssa_toolchain/analysis/ssa_construct.py` |
| **Critical edge splitting** | `ssa_toolchain/analysis/ssa_destroy.py` |
| **Parallel copy sequencing + cycle breaking with temporaries** | `ssa_toolchain/analysis/ssa_destroy.py` |
| Interpreter for raw / SSA / flat IR | `ssa_toolchain/interp.py` |
| SSA verifier (single definition, dominance, phi consistency, CFG) | `ssa_toolchain/analysis/verify.py` |
| JSON service (CLI + stdlib HTTP) | `ssa_toolchain/service.py` |
| Automated tests incl. random programs | `tests/` |

## Layout

```
ssa_toolchain/
  ast_nodes.py        AST node definitions (every node carries Loc)
  lexer.py            hand-written tokeniser
  parser.py           recursive-descent parser
  ir.py               IR data structures + text dumper
  ir_parser.py        IR text parser (raw / SSA / flat round-trips)
  interp.py           interpreter: memory / ssa / flat modes
  pipeline.py         source -> raw -> SSA -> flat driver + JSON view
  service.py          JSON request service (CLI, stdin, HTTP)
  frontend/builder.py AST -> non-SSA IR (slots via load/store)
  analysis/
    dominance.py      reachability, idoms, dominator tree, frontiers
    ssa_construct.py  phi insertion + renaming
    ssa_destroy.py    critical edges + parallel copies / cycles
    verify.py         SSA integrity checks
examples/             .mini sources + examples/requests/*.json
tests/                unittest suite (run from repo root)
```

## Requirements

Python 3.10+ (uses `X | Y` annotations). Standard library only; nothing
to install.

## Quick start

```bash
# compile + execute + compare all three IR stages, print JSON:
python3 -m ssa_toolchain.service --file examples/diamond.mini --input 5

# compile an example directly (--input is a comma separated arg list):
python3 -m ssa_toolchain.service --file examples/sum_loop.mini --input 10

# JSON request file:
python3 -m ssa_toolchain.service \
  --request examples/requests/unreachable.json

# stdin:
cat examples/requests/swap_loop.json | python3 -m ssa_toolchain.service

# HTTP service (stdlib only):
python3 -m ssa_toolchain.service --serve 8080
curl -s localhost:8080/compile -d @examples/requests/diamond.json
```

Request fields: `source` (mini text) or `file` (CLI only), `entry`
(default `main`), `inputs` (integer argument list), `execute`,
`include_ir`. Responses are `{"ok": true, "result": ...}` or
`{"ok": false, "error": {kind, message, loc, rendered}}`.

The `result` includes, per function: promoted variables, pruned
unreachable blocks, phi insertion sites, split critical edges, swap
temporaries, verifier notes, dominance summary and the three IR dumps
(`ir_raw`, `ir_ssa`, `ir_flat`), plus an `executions` block comparing
memory-mode interpretation of the original IR with SSA-mode and flat-mode
execution (`agree: true`).

## IR overview

Pre-SSA (variables are slots with explicit `load` / `store`):

```
entry:
  %c1 = const 1
  %t0 = load n
  %t1 = > %t0, %c1
  br %t1, b.if1.then, b.if1.else
b.if1.then:
  ...
  store %t3 -> x
  jmp b.if1.merge
b.if1.merge:
  %t6 = load x
  ret %t6
```

SSA (phis and terminator edge arguments; every value defined once):

```
b.if1.merge:
  %x.phi0 = phi [b.if1.then %t3] [b.if1.else %t5]
  ret %x.phi0
```

Flat / executable (phis eliminated to copies; critical edges split into
`b.edge*` blocks; cycles broken with `%cswapN`):

```
b.while1.body:
  %t7 = + %i.phi2, %c0
  %i.phi2 = copy %t7
  %cswap1 = copy %a.phi0
  %a.phi0 = copy %b.phi1
  %b.phi1 = copy %cswap1
  jmp b.while1.cond
```

## Algorithms, briefly

1. **Dominance.** Reachability from entry; reverse postorder DFS;
   Cooper–Harvey–Kennedy iterative fixed point for immediate dominators;
   dominator sets by idom-chain closure; dominance frontiers with the
   standard `DF` worklist rule.
2. **SSA construction (mem2reg / Cytron).** Prune unreachable blocks;
   collect variables from parameters/stores/loads; insert empty phis at
   iterated dominance frontiers of definition blocks; rename in
   dominator-tree preorder with one value stack per variable (parameters
   seed the stack, loads read it and disappear, stores push and
   disappear, phi results push); rewrite terminator edge arguments and
   rebuild phi incoming lists from them.
3. **SSA destruction.** Split every critical edge `pred -> succ` whose
   successor holds phis, moving edge arguments into the new edge block;
   gather, per edge, the full parallel copy for all destination phis;
   sequence each parallel copy by marking functional-graph cycles,
   emitting acyclic copies first, and implementing each cycle (and its
   in-tree) with temporaries, so the result is plain sequential `copy`
   instructions with identical semantics.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

Coverage highlights: diamond control flow; loop-carried variables;
unreachable block pruning; swap / 3-cycle / cycle-with-in-tree parallel
copies; a hand-written non-structural IR with two critical edges; IR text
round-trip; short-circuit logic; function calls; parser diagnostics; HTTP
service; 60 random structured programs checked for cross-mode equivalence
and single-definition validity.
