# 调度时间线（实际运行输出）

下列表格由 `cmd/pim` 对内置场景的实际运行结果生成（见 output/*.json）。

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

## 多锁释放顺序 R2-first

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
| 6 | lock_release | Low releases R2 |
| 6 | lock_grant | R2 granted to B (from Low) |
| 6 | task_wakeup | B wakes ready |
| 6 | priority_change | Low eff pri 4 -> 3 (grant) donors=['A'] |
| 6 | task_preempted | Low preempted by B |
| 6 | task_dispatch | -> dispatch B (eff 4) |
| 7 | tick | run B (burst left 0) |
| 7 | lock_release | B releases R2 |
| 7 | task_exit | B EXITS at 7 |
| 7 | task_dispatch | -> dispatch Low (eff 3) |
| 8 | tick | run Low (burst left 0) |
| 8 | lock_release | Low releases R1 |
| 8 | lock_grant | R1 granted to A (from Low) |
| 8 | task_wakeup | A wakes ready |
| 8 | priority_change | Low eff pri 3 -> 1 (grant) donors=[] |
| 8 | task_preempted | Low preempted by A |
| 8 | task_dispatch | -> dispatch A (eff 3) |
| 9 | tick | run A (burst left 0) |
| 9 | lock_release | A releases R1 |
| 9 | task_exit | A EXITS at 9 |
| 9 | task_dispatch | -> dispatch Low (eff 1) |
| 10 | tick | run Low (burst left 0) |
| 10 | task_exit | Low EXITS at 10 |

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
