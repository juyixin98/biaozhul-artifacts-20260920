;; Definitive failure fixture: destination is unreachable.  There is no road
;; leaving the depot at all, so neither m-direct nor m-via-hub can apply.
;; Expected category: no_applicable_method, with BOTH named methods present
;; in rejected_methods and the rejecting literal recorded for each.
(:problem logistics-unreachable (:domain logistics)
  (:init (at crate4 depot)
         (at truck1 depot)
         (free truck1))
  (:tasks (deliver crate4 truck1 depot moon)))
