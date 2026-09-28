;; Endpoints looked up from the manifest: (ship ?p ?v) introduces ?from and
;; ?to via existential preconditions, then decomposes to a direct delivery.
(:problem logistics-ship (:domain logistics)
  (:init (road depot port)
         (origin crate3 depot)
         (dest crate3 port)
         (at crate3 depot)
         (at truck2 depot)
         (free truck2))
  (:tasks (ship crate3 truck2)))
