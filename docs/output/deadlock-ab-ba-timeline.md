## AB/BA 死锁反例（检测）

makespan=1 inversionTicks=0 deadlock={'cycle': ['T1', 'T2', 'T1'], 'at': 1} error=''

| tick | event | detail |
|---:|---|---|
| 0 | task_arrive | T1 arrives pri=3 |
| 0 | task_dispatch | -> dispatch T1 (eff 3) |
| 0 | lock_acquire | T1 acquires R1 |
| 1 | tick | run T1 (burst left 0) |
| 1 | task_arrive | T2 arrives pri=4 |
| 1 | task_preempted | T1 preempted by T2 |
| 1 | task_dispatch | -> dispatch T2 (eff 4) |
| 1 | lock_acquire | T2 acquires R2 |
| 1 | task_block | T2 BLOCKS on R1 (owner T1) |
| 1 | priority_change | T1 eff pri 3 -> 4 (block) donors=['T2'] |
| 1 | task_dispatch | -> dispatch T1 (eff 4) |
| 1 | task_block | T1 BLOCKS on R2 (owner T2) |
| 1 | deadlock | DEADLOCK cycle=['T1', 'T2', 'T1'] |

