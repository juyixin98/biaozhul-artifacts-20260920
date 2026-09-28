;; Direct delivery: a single road A->B lets m-direct apply.  This fixture
;; exercises method mutual exclusion: m-via-hub is rejected because its
;; (not (road A B)) guard is false, and m-direct is chosen.
(:problem logistics-direct (:domain logistics)
  (:init (road depot port)
         (at crate1 depot)
         (at truck1 depot)
         (free truck1))
  (:tasks (deliver crate1 truck1 depot port)))
