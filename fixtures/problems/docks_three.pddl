;; Three workers, one lock, with a partial constraint: worker0 must precede
;; worker2; worker1 floats.  Planner status: success; verifier must confirm
;; the plan is one of the feasible linearizations and respects 0<2.
(:problem docks-three (:domain docks)
  (:init (free lock-1) (worker alpha) (worker beta) (worker gamma))
  (:tasks (three-work lock-1 alpha beta gamma)))
