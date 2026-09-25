## 经典优先级反转（无继承）

makespan=14 inversionTicks=4 deadlock=None error=''

| tick | event | detail |
|---:|---|---|
| 0 | task_arrive | Low arrives pri=1 |
| 0 | task_dispatch | -> dispatch Low (eff 1) |
| 1 | tick | run Low (burst left 0) |
| 1 | lock_acquire | Low acquires R |
| 2 | tick | run Low (burst left 5) |
| 2 | task_arrive | High arrives pri=3 |
| 2 | task_preempted | Low preempted by High |
| 2 | task_dispatch | -> dispatch High (eff 3) |
| 2 | task_block | High BLOCKS on R (owner Low) |
| 2 | task_dispatch | -> dispatch Low (eff 1) |
| 3 | tick | run Low (burst left 4) |
| 4 | tick | run Low (burst left 3) |
| 4 | task_arrive | Medium arrives pri=2 |
| 4 | task_preempted | Low preempted by Medium |
| 4 | task_dispatch | -> dispatch Medium (eff 2) |
| 5 | tick | run Medium (burst left 3) |
| 6 | tick | run Medium (burst left 2) |
| 7 | tick | run Medium (burst left 1) |
| 8 | tick | run Medium (burst left 0) |
| 8 | task_exit | Medium EXITS at 8 |
| 8 | task_dispatch | -> dispatch Low (eff 1) |
| 9 | tick | run Low (burst left 2) |
| 10 | tick | run Low (burst left 1) |
| 11 | tick | run Low (burst left 0) |
| 11 | lock_release | Low releases R |
| 11 | lock_grant | R granted to High (from Low) |
| 11 | task_wakeup | High wakes ready |
| 11 | task_preempted | Low preempted by High |
| 11 | task_dispatch | -> dispatch High (eff 3) |
| 12 | tick | run High (burst left 1) |
| 13 | tick | run High (burst left 0) |
| 13 | lock_release | High releases R |
| 13 | task_exit | High EXITS at 13 |
| 13 | task_dispatch | -> dispatch Low (eff 1) |
| 14 | tick | run Low (burst left 0) |
| 14 | task_exit | Low EXITS at 14 |

