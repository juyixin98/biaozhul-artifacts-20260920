;; Non-progress cycle: spin keeps re-expanding itself under an unchanged
;; world.  Expected definitive category: cycle.
(:problem spin (:domain bounds)
  (:init (spinning z))
  (:tasks (spin z)))
