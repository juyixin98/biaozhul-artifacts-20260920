;; Two workers, one lock, unordered subtasks.  6 topological orders exist;
;; only the 2 non-overlapping ones are executable.  Planner status: success.
(:problem docks-two (:domain docks)
  (:init (free lock-1) (worker alice) (worker bob))
  (:tasks (parallel-work lock-1 alice bob)))
