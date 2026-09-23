# Run log

All commands were executed on Linux (Python 3.12.3, standard library only;
no third-party packages required).

## Environment

```
$ python3 --version
Python 3.12.3
```

## Automated tests

Command:

```
$ python3 -m unittest discover -s tests
Ran 31 tests in 14.9s
OK
```

(or `./run_tests.sh`, which additionally prints the CLI demos below.)

### Acceptance: exhaustive bounded inputs vs. real execution

Each row runs the concrete reference interpreter on the full Cartesian
product of the input ranges and compares the observed first-fault locations
to the analyzer's alarms (`missed` = soundness violations; `false+` =
conservative false alarms).

```
case                                 runs precision  faults  alarms  missed  false+
p1_safe_loop_exact                      6 exact           0       0       0       0
p2_oob_loop_exact                       6 exact           1       1       0       0
p4_positive_guard_exact                 6 exact           0       0       0       0
p5_negative_index_exact                 5 exact           1       1       0       0
p7_definite_divzero_exact               1 exact           1       1       0       0
p9_arithmetic_exact                    16 exact           1       1       0       0
p10_load_store_oob_exact                6 exact           2       2       0       0
p11_nested_loops_exact                  6 exact           0       0       0       0
p3_diseq_guard_imprecise                5 imprecise       0       1       0       1
p6_correlation_imprecise                3 imprecise       0       1       0       1
```

### Randomized differential fuzzing

```
[fuzz] 1500/1500 valid generated programs, 37500 concrete runs,
       1012 programs with conservative (false) alarms, 0 unsound
```

The interval-arithmetic property tests additionally check abstract `/`,`%`,
`*`,`+`,`-` against every point combination over small finite intervals and
over 3,000 random intervals with infinite endpoints — no containment
violation was found.

## CLI demos

Safe loop (n ∈ [0,5], array size 5):

```
$ python3 -m intervalai.cli analyze examples/p1_safe_loop.imp --bound n=0..5
alarms              : 0
exit scalars        : i=[0, 5], n=[0, 5]
exit array a[5] elements: [0, 4]
no possible division-by-zero or array OOB detected
```

Loop that can exceed the array size (n ∈ [0,5], array size 3):

```
$ python3 -m intervalai.cli analyze examples/p2_oob_loop.imp --bound n=0..5
alarms              : 1
  - [possible] index_out_of_bounds/index_too_large_possible at line 11:
    array index may be >= array size 3
exit scalars        : i=[0, 5], n=[0, 5]
```

Fully unknown input (no bound ⇒ `[-oo,+oo]`):

```
$ python3 -m intervalai.cli analyze examples/p8_unbounded_loop.imp
alarms              : 1
  - [possible] index_out_of_bounds/index_too_large_possible at line 12:
    array index may be >= array size 3
exit scalars        : i=[0, +oo], n=[-oo, +oo]
```

Nested loops, unknown input (selective widening; the accumulator, induction
variables and array summary reach +oo, and the store is alarmed):

```
$ python3 -m intervalai.cli analyze examples/p11_nested_loops.imp
alarms              : 1
  - [possible] index_out_of_bounds/index_too_large_possible at line 19:
    array index may be >= array size 5
exit scalars        : i=[0, +oo], j=[0, +oo], n=[-oo, +oo], s=[0, +oo]
exit array a[5] elements: [0, +oo]
```

Concrete execution:

```
$ python3 -m intervalai.cli execute examples/p2_oob_loop.imp -D n=2
scalar i = 2
scalar n = 2
array  a = [0, 1, 2]
```

## JSON service

stdio line protocol:

```
$ (cat examples/req_analyze_safe.json; cat examples/req_analyze_oob.json; \
   cat examples/req_execute.json) | python3 -m intervalai.service
analyze: status ok alarms 0
analyze: status ok alarms 1
execute: scalars {'i': 4, 'n': 4}
```

HTTP:

```
$ python3 -m intervalai.service --http --port 8000
$ curl -s -X POST http://127.0.0.1:8000/analyze \
    -H 'Content-Type: application/json' --data @examples/req_analyze_oob.json
# -> 200 with the index_out_of_bounds alarm at source line 5
```

## Failures encountered during development (now fixed)

Building the exhaustive oracle and fuzzer surfaced genuine soundness bugs;
all are fixed and regression-covered:

1. A partial operation that can fault (`x/0`, OOB array read) returned
   bottom, which marked the whole continuation unreachable and masked later
   alarms. Fixed: the faulting result is joined with TOP.
2. An out-of-bounds array *load* used the in-bounds element summary instead
   of an unknown value. Fixed: possible-OOB loads yield TOP.
3. The interval `meet` (intersection) was implemented incorrectly. Fixed.
4. Interval `widen` did not retain an already-infinite bound. Fixed to the
   standard rule.
5. Inner-loop widening spuriously widened outer-loop induction variables.
   Fixed with dominator/natural-loop based *selective* widening.
6. The differential oracle originally continued through a first fault with
   placeholders, fabricating later "faults". Replaced with an enumeration of
   first faults of error-free execution prefixes.

There are currently **no known failing tests or unaddressed items**.
