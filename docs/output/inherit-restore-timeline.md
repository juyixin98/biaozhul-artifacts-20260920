## 继承后释放恢复基础优先级

makespan=7 inversionTicks=0 deadlock=None error=''

| tick | event | detail |
|---:|---|---|
| 0 | task_arrive | Low arrives pri=1 |
| 0 | task_dispatch | -> dispatch Low (eff 1) |
| 1 | tick | run Low (burst left 0) |
| 1 | lock_acquire | Low acquires R |
| 2 | tick | run Low (burst left 2) |
| 2 | task_arrive | High arrives pri=4 |
| 2 | task_preempted | Low preempted by High |
| 2 | task_dispatch | -> dispatch High (eff 4) |
| 2 | task_block | High BLOCKS on R (owner Low) |
| 2 | priority_change | Low eff pri 1 -> 4 (block) donors=['High'] |
| 2 | task_dispatch | -> dispatch Low (eff 4) |
| 3 | tick | run Low (burst left 1) |
| 4 | tick | run Low (burst left 0) |
| 4 | lock_release | Low releases R |
| 4 | lock_grant | R granted to High (from Low) |
| 4 | task_wakeup | High wakes ready |
| 4 | priority_change | Low eff pri 4 -> 1 (grant) donors=[] |
| 4 | task_preempted | Low preempted by High |
| 4 | task_dispatch | -> dispatch High (eff 4) |
| 5 | tick | run High (burst left 0) |
| 5 | lock_release | High releases R |
| 5 | task_exit | High EXITS at 5 |
| 5 | task_dispatch | -> dispatch Low (eff 1) |
| 6 | tick | run Low (burst left 1) |
| 7 | tick | run Low (burst left 0) |
| 7 | task_exit | Low EXITS at 7 |

