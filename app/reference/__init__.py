"""Independent reference oracle (deliberately separate from app.core).

Modules here re-implement, from scratch and using only numpy + the data-only
fixture specs:

* eager forward/backward differentiation (:mod:`oracle`)
* central finite differences (:mod:`finite_diff`)
* an independent checkpoint liveness/peak-memory simulator
  (:mod:`liveness`)
* the documented per-node dropout seeding rule (:mod:`seedspec`)

The test suite treats these as ground truth.  They must not import planner,
executor or memory accounting code from :mod:`app.core`.
"""
