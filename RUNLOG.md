# Run log

Environment: Python 3.12.3, Linux 6.8.0-90-generic x86_64
Date: 2026-09-23T11:57:04Z

All commands were executed from the repository root. Standard
library only - no dependencies were installed.

## 1. Automated tests

```
........................
----------------------------------------------------------------------
Ran 71 tests in 0.679s

OK
```

## 2. SSA single-definition acceptance script

```
diamond.mini :: main: 8 distinct SSA values, 8 operand uses, 0 pruned blocks
  single static definition: PASS
  verifier (dominance + phi consistency): PASS
  raw/SSA/flat agree on [5]: return=6 PASS
sum_loop.mini :: main: 9 distinct SSA values, 8 operand uses, 0 pruned blocks
  single static definition: PASS
  verifier (dominance + phi consistency): PASS
  raw/SSA/flat agree on [10]: return=55 PASS
unreachable.mini :: main: 8 distinct SSA values, 5 operand uses, 3 pruned blocks
  single static definition: PASS
  verifier (dominance + phi consistency): PASS
  raw/SSA/flat agree on [0]: return=20 PASS
swap_loop.mini :: main: 12 distinct SSA values, 9 operand uses, 0 pruned blocks
  single static definition: PASS
  verifier (dominance + phi consistency): PASS
  raw/SSA/flat agree on [4]: return=8 PASS
nested.mini :: abs: 5 distinct SSA values, 6 operand uses, 1 pruned blocks
  single static definition: PASS
  verifier (dominance + phi consistency): PASS
nested.mini :: main: 19 distinct SSA values, 17 operand uses, 0 pruned blocks
  single static definition: PASS
  verifier (dominance + phi consistency): PASS
  raw/SSA/flat agree on [7]: return=21 PASS
OVERALL: PASS
```

## 3. Cross-mode execution matrix (raw IR vs SSA vs phi-free)

```
diamond     inputs=[-4]   ret=   -5 printed=[-5]         PASS
diamond     inputs=[0]    ret=   -1 printed=[-1]         PASS
diamond     inputs=[5]    ret=    6 printed=[6]          PASS
sum_loop    inputs=[0]    ret=    0 printed=[0]          PASS
sum_loop    inputs=[10]   ret=   55 printed=[55]         PASS
sum_loop    inputs=[100]  ret= 5050 printed=[5050]       PASS
unreachable inputs=[7]    ret=   20 printed=[20, 0]      PASS
swap_loop   inputs=[0]    ret=    8 printed=[1, 7]       PASS
swap_loop   inputs=[1]    ret=    8 printed=[7, 1]       PASS
swap_loop   inputs=[2]    ret=    8 printed=[1, 7]       PASS
swap_loop   inputs=[3]    ret=    8 printed=[7, 1]       PASS
nested      inputs=[10]   ret=   45 printed=[45]         PASS
```
