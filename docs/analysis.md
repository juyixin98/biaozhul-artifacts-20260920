# Interval abstract interpretation — design

This document describes how `intervalai` approximates program behavior. The
implementation is in `intervalai/intervals.py`, `abstract_env.py`,
`ir.py`, and `analyzer.py`.

## 1. The abstract domain

An abstract integer is an interval

```
[l, h]    with l ∈ Z ∪ {-∞}, h ∈ Z ∪ {+∞}, l <= h
```

plus a bottom element `⊥` denoting the empty set of integers (an unreachable
program point). There is no top constant; top is represented by
`[-∞, +∞]`.

Lattice operations:

* **join (⊔)** — convex hull: `[a,b] ⊔ [c,d] = [min(a,c), max(b,d)]`;
* **meet (⊓)** — intersection: `[max(a,c), min(b,d)]` (⊥ if disjoint);
* ordering — interval containment.

Every element is one interval, so the domain is non-relational: it cannot
represent relationships between different variables.

## 2. Abstract arithmetic over ℤ

All arithmetic is exact Python-integer arithmetic (mathematical integers;
there is deliberately no machine width and no overflow to model).

* `+`, `-`: endpoint formulas (exact).
* `*`: products of the four corner pairs for finite intervals (exact); a
  sign-split rule handles infinite endpoints soundly.
* `/`, `%`: floor division / floor remainder. The divisor is split into a
  strictly-positive piece and a strictly-negative piece (0 is never a valid
  divisor). For finite pieces, quotient extrema come from the corner pairs
  (floor division is monotone in the numerator for a fixed divisor sign);
  remainder extrema come from corner pairs plus the generic
  `-(|b|-1) … |b|-1` envelope. Infinite endpoints propagate to ±∞ with the
  sign dictated by the divisor; a divisor whose magnitude is itself
  unbounded collapses finite numerators to a neighborhood of 0.

The abstract result **contains** every concrete result — this is verified
exhaustively over small intervals in `tests/test_intervals.py`, and on
thousands of generated programs in `tests/test_fuzz.py`.

## 3. Arrays: smashing with weak updates

An array of static size `n` is summarized by a **single** element interval
(`array smashing`): every cell shares one abstraction. A store performs a
*weak update* — the summary becomes `old ⊔ stored`, which is sound but loses
per-cell information. Bounds checks use the index interval against the
literal size.

## 4. Conditions and refinement (`assume`)

On reaching a branch, the incoming environment is refined for the taken and
not-taken edges:

* comparisons against a provably constant expression intersect the
  variable's interval with the implied half-line (e.g. `x > c ⇒ x >= c+1`);
* comparisons between two variables propagate each side's known bound
  (`x < y` with `y <= 5` gives `x <= 4`; and symmetrically
  `y >= min(x)+1`), which is what makes loop-index arguments precise;
* `and`/`or`/`not` push the refinement into the operands (with joins on
  disjunctive branches);
* equality intersects intervals; disequality only removes a boundary
  singleton (an interior hole cannot be represented).

An unrefinable condition leaves the state unchanged — refinement is a
precision device and never removes feasible states.

## 5. IR and CFG

The resolved AST is lowered to an integer IR (`ir.py`) of basic blocks:

```
Const, Copy, LoadElem, StoreElem, Unary, Bin, SetVar
terminators: Jump | Branch(cond, yes, no) | Halt
```

Expressions are flattened into temporaries `%t0, %t1, …`; branch conditions
keep their typed AST node so the transfer function can evaluate the condition
and apply `assume` on each edge. The CFG records successors/predecessors, a
DFS reverse post-order, and **back-edge targets = loop headers**.

## 6. Fixpoint: widening then narrowing

Loops make the CFG cyclic, so plain joins may never stabilize (an induction
variable can grow by one forever on paper). The analyzer runs:

1. **Ascending iteration with widening.** A worklist processes blocks; at a
   loop header the new incoming join is combined with the previous post point
   using
   ```
   widen([a,b], [c,d]) = [ a==None or c<a ? -∞ : a,
                           b==None or d>b ? +∞ : b ]
   ```
   i.e. any bound that increases/decreases at a header jumps straight to
   infinity, guaranteeing termination. Widening is **selective**: the CFG is
   analyzed for dominators and natural loops, and at each header only the
   scalar variables modified inside that header's natural loop are widened;
   variables the loop leaves invariant are joined, not widened. Without this,
   an outer induction variable passing through an *inner* loop header gets
   spuriously widened (the inner header re-sees it grow on outer iterations).
   The program entry participates as a virtual incoming edge (so a `while`
   that is also the CFG entry block is handled correctly).
2. **Descending iteration with narrowing.** After the post-fixpoint, bounded
   reverse-post-order rounds recompute states and refine header states with
   ```
   narrow([a,b],[c,d]) = [ a==None ? c : a, b==None ? d : b ]
   ```
   pulling infinite bounds down to the stable finite bound (e.g. the real
   loop upper limit). Narrowing is guarded so the result is always a sub-state
   of the current sound post-fixpoint; it therefore only sharpens, never
   excludes feasible states.
3. **Alarm collection.** A final pass records per-block entry environments
   and raises alarms.

### Partial operations and why a fault yields TOP, not BOTTOM

Division/remainder by zero and out-of-bounds array reads are *partial*
operations. When the abstract operand intersects an error region, the
continuation must model the **undefined result on the error path** as
`TOP`/unknown (joined with the result over valid operands) — never as bottom.
Using bottom would mark all code after a possibly-faulting operation
unreachable and silently suppress every later alarm. Concretely:

* `/`,`%` with a divisor interval containing 0 return `quotient ⊔ TOP`;
* an array load whose index interval may be out of bounds yields the element
  summary joined with `TOP` (an OOB read produces an arbitrary/undefined
  value).

This rule was the decisive soundness property discovered through the
exhaustive randomized differential tests.

## 7. Alarms: unknown is never "safe"

Two fault classes are reported, each with a source location and the offending
abstract interval:

* `div_by_zero` — for every `/`/`%` whose divisor interval contains 0;
  severity `definite` if the divisor is exactly `[0,0]`, else `possible`.
* `index_out_of_bounds` — separate `negative_index` (lo < 0, including lo
  = -∞) and `index_too_large` (hi ≥ size, including hi = +∞) sub-alarms,
  each `definite` when the whole interval is on the illegal side, otherwise
  `possible`.

Crucially, a fully unknown interval `[-∞,+∞]` (an unconstrained input or an
expression the domain cannot bound) intersects every error region, so it
raises the corresponding *possible* alarms. The analysis never equates "we
do not know" with "it is safe".

## 8. Conservativity boundaries (known imprecision)

The interval domain is intentionally simple. Two structural sources of false
positives are demonstrated and tested:

* **No relational information.** `y = x; … x - y + 1` is always 1, but
  `[-1,1] - [-1,1] + 1 = [-1,3]` contains 0, so a guarded division is still
  warned (`examples/p6_correlation.imp`).
* **No set-union / no interior holes.** After `d != 0` with `d ∈ [-2,2]`,
  the true set is `{-2,-1,1,2}`, which a single interval cannot express; the
  interval stays `[-2,2]`, so a subsequent division by `d` is still warned
  (`examples/p3_diseq_guard.imp`).

Array smashing adds a third, milder loss: writing precise values to one cell
is merged into the summary covering all cells. None of these can produce a
false *negative*: widening, joins, weak updates, and skipped refinements only
ever enlarge the abstract set.

## 9. Soundness methodology

The acceptance harness (`tests/diff_engine.py`, `tests/test_acceptance.py`,
`tests/test_fuzz.py`) builds the Cartesian product of bounded input ranges,
runs an **independent concrete AST interpreter** on each input to observe
real faults, and requires:

* every observed fault must be covered by an analyzer alarm (no misses);
* hand-written "exact" cases must additionally have no false alarm;
* hand-written "imprecise" cases must exhibit the specific, documented false
  alarm (marking the conservativity boundary).

The concrete interpreter walks the AST directly (a different code path from
the IR/analyzer), so the oracle and the analysis can share a bug only through
the parser/resolver, which has its own tests.

The oracle enumerates *first faults of error-free execution prefixes*
(`collect_prefix_faults` in `concrete.py`): a single execution aborts at its
first fault, so naively continuing with a placeholder value would fabricate
"faults" on operations only reachable after an earlier fault (masked by that
fault). Instead each already-discovered fault location is treated as already
past in a fresh replay, and the first *new* fault is recorded, iterating to a
fixed point. This yields exactly the operations that genuinely fault on a
clean prefix and matches the analyzer's *may*-alarm semantics one-to-one.
