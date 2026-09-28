;; Multi-hop delivery: no direct depot->pier road, so m-direct is rejected
;; and m-via-hub recurses depot -> port -> pier.  Requires at least depth 3.
(:problem logistics-recursive (:domain logistics)
  (:init (road depot port)
         (road port pier)
         (at crate2 depot)
         (at truck1 depot)
         (free truck1))
  (:tasks (deliver crate2 truck1 depot pier)))
