;; Definitive deadlock fixture: both claimers need the one permit and it is
;; never released.  The task network is a valid DAG (two unordered nodes) but
;; every linearization stalls after the first grab.  Expected planner
;; category: deadlock; independent verifier feasible_linearizations = 0
;; out of 2 examined.
(:problem permit-two (:domain permit-deadlock)
  (:init (free permit-1) (claimer p1) (claimer p2))
  (:tasks (claim-both permit-1 p1 p2)))
