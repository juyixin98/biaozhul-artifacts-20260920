## 优先级继承修复反转

makespan=14 inversionTicks=0 deadlock=None error=''

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
| 2 | priority_change | Low eff pri 1 -> 3 (block) donors=['High'] |
| 2 | task_dispatch | -> dispatch Low (eff 3) |
| 3 | tick | run Low (burst left 4) |
| 4 | tick | run Low (burst left 3) |
| 4 | task_arrive | Medium arrives pri=2 |
| 5 | tick | run Low (burst left 2) |
| 6 | tick | run Low (burst left 1) |
| 7 | tick | run Low (burst left 0) |
| 7 | lock_release | Low releases R |
| 7 | lock_grant | R granted to High (from Low) |
| 7 | task_wakeup | High wakes ready |
| 7 | priority_change | Low eff pri 3 -> 1 (grant) donors=[] |
| 7 | task_preempted | Low preempted by High |
| 7 | task_dispatch | -> dispatch High (eff 3) |
| 8 | tick | run High (burst left 1) |
| 9 | tick | run High (burst left 0) |
| 9 | lock_release | High releases R |
| 9 | task_exit | High EXITS at 9 |
| 9 | task_dispatch | -> dispatch Medium (eff 2) |
| 10 | tick | run Medium (burst left 3) |
| 11 | tick | run Medium (burst left 2) |
| 12 | tick | run Medium (burst left 1) |
| 13 | tick | run Medium (burst left 0) |
| 13 | task_exit | Medium EXITS at 13 |
| 13 | task_dispatch | -> dispatch Low (eff 1) |
| 14 | tick | run Low (burst left 0) |
| 14 | task_exit | Low EXITS at 14 |

