## 三级嵌套传递继承

makespan=12 inversionTicks=0 deadlock=None error=''

| tick | event | detail |
|---:|---|---|
| 0 | task_arrive | Low arrives pri=1 |
| 0 | task_dispatch | -> dispatch Low (eff 1) |
| 0 | lock_acquire | Low acquires R1 |
| 1 | tick | run Low (burst left 7) |
| 1 | task_arrive | High arrives pri=3 |
| 1 | task_preempted | Low preempted by High |
| 1 | task_dispatch | -> dispatch High (eff 3) |
| 1 | lock_acquire | High acquires R2 |
| 1 | task_block | High BLOCKS on R1 (owner Low) |
| 1 | priority_change | Low eff pri 1 -> 3 (block) donors=['High'] |
| 1 | task_dispatch | -> dispatch Low (eff 3) |
| 2 | tick | run Low (burst left 6) |
| 2 | task_arrive | Urgent arrives pri=4 |
| 2 | task_preempted | Low preempted by Urgent |
| 2 | task_dispatch | -> dispatch Urgent (eff 4) |
| 2 | task_block | Urgent BLOCKS on R2 (owner High) |
| 2 | priority_change | Low eff pri 3 -> 4 (block) donors=['High', 'Urgent'] |
| 2 | priority_change | High eff pri 3 -> 4 (block) donors=['Urgent'] |
| 2 | task_dispatch | -> dispatch Low (eff 4) |
| 3 | tick | run Low (burst left 5) |
| 4 | tick | run Low (burst left 4) |
| 5 | tick | run Low (burst left 3) |
| 6 | tick | run Low (burst left 2) |
| 7 | tick | run Low (burst left 1) |
| 8 | tick | run Low (burst left 0) |
| 8 | lock_release | Low releases R1 |
| 8 | lock_grant | R1 granted to High (from Low) |
| 8 | task_wakeup | High wakes ready |
| 8 | priority_change | Low eff pri 4 -> 1 (grant) donors=[] |
| 8 | task_exit | Low EXITS at 8 |
| 8 | task_dispatch | -> dispatch High (eff 4) |
| 9 | tick | run High (burst left 1) |
| 10 | tick | run High (burst left 0) |
| 10 | lock_release | High releases R1 |
| 10 | lock_release | High releases R2 |
| 10 | lock_grant | R2 granted to Urgent (from High) |
| 10 | task_wakeup | Urgent wakes ready |
| 10 | priority_change | High eff pri 4 -> 3 (grant) donors=[] |
| 10 | task_exit | High EXITS at 10 |
| 10 | task_dispatch | -> dispatch Urgent (eff 4) |
| 11 | tick | run Urgent (burst left 1) |
| 12 | tick | run Urgent (burst left 0) |
| 12 | lock_release | Urgent releases R2 |
| 12 | task_exit | Urgent EXITS at 12 |

