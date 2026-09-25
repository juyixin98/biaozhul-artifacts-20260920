## 多锁释放顺序 R1-first

makespan=10 inversionTicks=0 deadlock=None error=''

| tick | event | detail |
|---:|---|---|
| 0 | task_arrive | Low arrives pri=1 |
| 0 | task_dispatch | -> dispatch Low (eff 1) |
| 0 | lock_acquire | Low acquires R1 |
| 0 | lock_acquire | Low acquires R2 |
| 1 | tick | run Low (burst left 5) |
| 2 | tick | run Low (burst left 4) |
| 2 | task_arrive | A arrives pri=3 |
| 2 | task_preempted | Low preempted by A |
| 2 | task_dispatch | -> dispatch A (eff 3) |
| 2 | task_block | A BLOCKS on R1 (owner Low) |
| 2 | priority_change | Low eff pri 1 -> 3 (block) donors=['A'] |
| 2 | task_dispatch | -> dispatch Low (eff 3) |
| 3 | tick | run Low (burst left 3) |
| 3 | task_arrive | B arrives pri=4 |
| 3 | task_preempted | Low preempted by B |
| 3 | task_dispatch | -> dispatch B (eff 4) |
| 3 | task_block | B BLOCKS on R2 (owner Low) |
| 3 | priority_change | Low eff pri 3 -> 4 (block) donors=['A', 'B'] |
| 3 | task_dispatch | -> dispatch Low (eff 4) |
| 4 | tick | run Low (burst left 2) |
| 5 | tick | run Low (burst left 1) |
| 6 | tick | run Low (burst left 0) |
| 6 | lock_release | Low releases R1 |
| 6 | lock_grant | R1 granted to A (from Low) |
| 6 | task_wakeup | A wakes ready |
| 7 | tick | run Low (burst left 0) |
| 7 | lock_release | Low releases R2 |
| 7 | lock_grant | R2 granted to B (from Low) |
| 7 | task_wakeup | B wakes ready |
| 7 | priority_change | Low eff pri 4 -> 1 (grant) donors=[] |
| 7 | task_preempted | Low preempted by B |
| 7 | task_dispatch | -> dispatch B (eff 4) |
| 8 | tick | run B (burst left 0) |
| 8 | lock_release | B releases R2 |
| 8 | task_exit | B EXITS at 8 |
| 8 | task_dispatch | -> dispatch A (eff 3) |
| 9 | tick | run A (burst left 0) |
| 9 | lock_release | A releases R1 |
| 9 | task_exit | A EXITS at 9 |
| 9 | task_dispatch | -> dispatch Low (eff 1) |
| 10 | tick | run Low (burst left 0) |
| 10 | task_exit | Low EXITS at 10 |

