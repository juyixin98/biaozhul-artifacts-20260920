;; A progress chain longer than the horizon.  With max_depth=1 the planner
;; can perform the first hop a->b but cannot finish walk(b), so the verdict
;; must be 'inconclusive' (depth_budget), separated from definitive failure.
(:problem walk-long (:domain bounds)
  (:init (at a) (edge a b) (edge b c) (target c))
  (:tasks (walk a)))
