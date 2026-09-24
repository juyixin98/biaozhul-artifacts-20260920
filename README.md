# shmring — 单生产者/单消费者跨进程共享内存环形队列

纯后端 C++17 实现。固定容量、变长消息（创建时锁定上限），基于 POSIX
`shm_open` 共享内存 + `std::atomic` + Linux **futex** + **PTHREAD_ROBUST
进程共享互斥锁**。无任何第三方运行时依赖（只链接 glibc 的 `librt`/
`libpthread`）。

```
生产者进程 ── shm_open/mmap ──┐
                              ├── /dev/shm/shmring_<name> ──┐
消费者进程 ── shm_open/mmap ──┘                              │
                                                             ▼
              [Header][capacity 个固定步长 slot，内含变长 payload]
```

---

## 1. 快速开始

```bash
make            # 构建 3 个二进制（g++ -std=c++17 -O2）
make check      # 确认进程间原子无锁
make test       # 11 组、65 项自动化测试（全部用两个真实进程）
make demo       # 手工演示：producer/consumer 两进程传 examples/input.txt
```

验收命令（干净环境从零执行）：

```bash
make clean && make && make test
# 期望结尾： 65 passed, 0 failed
```

压力测试（100 万条消息、容量 8 → 回绕 12.5 万次）：

```bash
Q=stress
./shmring-ctl create  $Q --capacity 8 --max-msg 256
./shmring-consumer $Q --capacity 8 --max-msg 256 \
    --count 1000000 --timeout 120000 --quiet &
./shmring-producer $Q --capacity 8 --max-msg 256 \
    --gen 1000000 --gen-size 128 --timeout 120000 --quiet
./shmring-ctl info   $Q      # head==tail==1000000, torn_writes=0, corrupt=0
./shmring-ctl doctor $Q      # bad_slots=0 healthy
./shmring-ctl destroy $Q
```

本机实测约 **0.7 M msg/s**（128B 负载、cap=8）。

### 手工双终端示例

```bash
# 终端 A（消费者，最长等 30 秒）
./shmring-ctl create q1 --capacity 4 --max-msg 4096
./shmring-consumer q1 --capacity 4 --max-msg 4096 --timeout 30000

# 终端 B（生产者，从 stdin 逐行读，空行也是合法零长消息）
head -6 examples/input.txt | ./shmring-producer q1 \
    --capacity 4 --max-msg 4096 --stdin --flush

# 任意终端：查看状态 / CRC 审计 / 删除
./shmring-ctl info q1
./shmring-ctl doctor q1
./shmring-ctl destroy q1
```

---

## 2. 二进制与退出码

| 二进制 | 角色 |
|---|---|
| `shmring-producer` | 持有生产者租约；`--gen N` 合成消息或 `--stdin` 逐行 |
| `shmring-consumer` | 持有消费者租约；每条消息输出 `MSG seq=… len=… data=…` |
| `shmring-ctl` | `create` / `destroy` / `info` / `doctor` / `check` / `damage` |

CLI 退出码即库状态码：`0 OK`、`1 FULL(拒绝)`、`2 EMPTY`、`3 TIMEOUT`、
`4 TOO_LARGE`、`6 CORRUPT`、`7 配置不匹配`、`11 角色已被存活进程持有`、
`13 不存在`（完整列表见 `shmring.h`）。

关键参数：

- `--timeout MS`：`0` = 非阻塞（满即拒绝 `ERR_FULL`/空即 `ERR_EMPTY`）；
  `>0` = 最多阻塞毫秒数，超时返回 `ERR_TIMEOUT`；`-1` = 永久阻塞
  （仍可被 SIGINT/SIGTERM 打断返回 `ERR_INTERRUPTED`）。
- `--capacity N`：槽位数（≥2），创建后不可变。
- `--max-msg M`：单条消息字节上限（0 ~ 16 MiB），创建后不可变。
- 共享内存对象名固定为 `/shmring_<name>`（避免与系统现有对象冲突），
  权限 `0600`。

---

## 3. 协议与内存序

### 3.1 内存布局

```
Header: magic/version/capacity/max_msg/stride/size
        head(uint64) tail(uint64) 计数器（绝对序列号，不随回绕取模）
        torn_writes / full_waits / empty_waits / corrupt_events
        wake_free / wake_used   futex 通知字
        producer_lock / consumer_lock  (PTHREAD_PROCESS_SHARED|ROBUST)
Slot[i]: state(u32 atomic) len(u32) crc32(u32) seq(u64) payload[0..max_msg]
```

槽位逻辑状态机：

```
AVAILABLE ──生产者申领──▶ WRITING ──写完 payload/len/crc/seq 后 release──▶ COMMITTED
     ▲                                                                          │
     └────────────── release head 后回收 ◀── READING ◀──消费者 acquire 校验──────┘
```

### 3.2 发布 / 读取的 happens-before

所有跨进程字段都在 `MAP_SHARED` 映射中；`atomic<u32/u64>` 在 x86_64/
aarch64 上**无锁**（`make check` 与 `is_always_lock_free` 双重断言；
非无锁平台直接拒绝创建）。

- **发布**：生产者先写 `payload/len/seq/crc`（relaxed），再
  `state.store(COMMITTED, release)`，最后 `tail.store(+1, release)`
  并 futex 唤醒。
- **读取**：消费者 `tail.load(acquire)` 看到新游标后，对槽位
  `state.load(acquire)`，release/acquire 配对保证**消费者不可能读到
  COMMITTED 之前的任何负载字节**；随后校验 `seq == head` 与
  `CRC32(seq||len||payload)`，不通过立即返回 `ERR_CORRUPT`，队列冻结。
- **确认**：消费者先 `head.store(+1, release)` 再把槽位置 AVAILABLE；
  生产者 `head.load(acquire)` 且 `tail-head < capacity` 才会复用槽位，
  因此**未确认消息绝不会被覆盖**。
- 通知采用独立 futex 字的 epoch 递增（`release` 后 `FUTEX_WAKE`），
  等待端短自旋（128 次 pause）后按 20ms 分片 `FUTEX_WAIT_BITSET`，
  不使用 `FUTEX_PRIVATE_FLAG`（共享映射语义）。

### 3.3 序号回绕检测

`head/tail` 是 **uint64 绝对序列号**（不是取模下标），槽位下标才取模。
因此：

- 回绕只是 `seq % capacity` 复用同一物理槽；每条消息携带自己的绝对
  `seq`，消费者读到 `slot.seq != head` 即判定双重回绕/撕裂，返回
  `ERR_CORRUPT`，不会把上一轮的陈旧数据当成新消息。
- 积压深度用无符号差值 `tail - head` 判定，天然处理回绕；序列号空间
  耗尽（2^64）返回 `ERR_OVERFLOW`。

### 3.4 满 / 空策略

- 满：`--timeout 0` 立即 `ERR_FULL=1`（拒绝策略，已提交消息不动）；
  `--timeout >0` futex 阻塞到有空闲槽或超时 `ERR_TIMEOUT=3`；`-1`
  阻塞直到可写或收到信号。空侧同理。
- 等待计数器 `full_waits/empty_waits` 可在 `info` 中观测。

---

## 4. 进程崩溃与半写槽恢复（不静默丢消息）

两个角色各持一把 **robust 进程共享互斥锁（租约）**。进程死亡（含
`SIGKILL`/掉电）后内核标记锁持有者死亡，后继进程 `pthread_mutex_lock`
收到 `EOWNERDEAD`，在同一临界区执行恢复，再 `pthread_mutex_consistent`。

| 崩溃点 | 现场 | 恢复动作 | 消息语义 |
|---|---|---|---|
| 生产者死在 `WRITING`（半写槽） | `tail` 槽 state=WRITING，尾部只有部分字节 | 槽位置回 AVAILABLE、**tail 不前进**、`torn_writes++`；后继生产者复用同一序列号重发 | 该消息从未提交，**已确认消息零丢失** |
| 消费者死在 `READING`（已复制未确认） | `head` 槽 state=READING，head 未动 | 槽位置回 COMMITTED，重启消费者**重新投递** | **至少一次**（at-least-once），payload CRC 校验一致 |
| 结构不一致（游标倒置、槽状态非法、CRC 坏） | — | 恢复回调标记 inconsistent，锁被标记 NOTRECOVERABLE，`open` 返回 `ERR_CORRUPT` | **明确要求重新初始化**（`destroy`+`create`），绝不静默跳过/丢弃 |

恢复刻意只检查并修复**本角色游标处的那一个槽**：另一角色可能仍然存活
（两把锁相互独立），全表扫描会把存活对方正在写的槽误判为损坏。

测试钩子（仅供夹具使用）：

- 生产者 `--kill-at SEQ --kill-after-bytes B`：申领槽、拷贝 B 字节后
  原地 `raise(SIGKILL)`，确定性留下半写槽。
- 消费者 `--kill-at N --kill-after-bytes B`：翻 READING、拷出 B 字节后
  `SIGKILL`，确定性制造“未确认”窗口。

---

## 5. “进程共享”与“线程内原子假设”的区别

1. **无锁性是硬前提**：进程间不能依赖“同一进程内的锁”。启动时断言
   `atomic<u32/u64>::is_always_lock_free`；futex 与 mutex 均带进程共享
   属性，**不带** `FUTEX_PRIVATE_FLAG`。
2. **同步媒介是共享映射而非线程内存**：release/acquire 作用于
   `MAP_SHARED` 页缓存；普通互斥锁/条件变量在 `fork`/独立进程后毫无
   意义，因此用 robust `PTHREAD_PROCESS_SHARED` 锁与内核 futex。
3. **崩溃模型不同**：线程崩溃通常拖垮整个进程、内存一起消失；进程崩溃
   后共享内存与锁依然存在，必须能识别“前持有者死亡”（robust mutex 的
   `EOWNERDEAD`）并修复槽状态——本项目的恢复路径即为此设计。
4. **角色互斥在进程粒度**：第二个存活生产者 `open` 时阻塞在生产者
   租约上（测试 10 验证），而不是靠线程 ID 约束。

---

## 6. 自动化测试

`python3 tests/run_tests.py`（仅用标准库），**每个用例都启动两个真实
OS 进程**，不做任何进程内/线程模拟：

| # | 内容 |
|---|---|
| 01 | 平台原子无锁断言 |
| 02 | 双进程 FIFO、变长、空行、UTF-8 字节精确一致 |
| 03 | cap=16 高频回绕：2 万条计数 + 5000 条逐字节校验 |
| 04 | 满队列拒绝（退出码 1）与阻塞超时（退出码 3）、腾出空间后继续 |
| 05 | 生产者 SIGKILL 留半写槽 → tail 不前进、4 条已提交消息完整、继任者复用序列号、`torn_writes=1` |
| 06 | 消费者 SIGKILL 在确认窗口 → 重启后 seq0 原样重投 |
| 07 | 完整重启夹具：杀掉长跑生产者(P1)→重启生产者(P2)→杀掉消费者(C1)→重启消费者(C2)，最终 head==tail==3000 |
| 08 | `damage` 翻转已提交负载 → CRC 拦截、队列冻结 head=1、doctor 报 1 坏槽、显式重建后可用 |
| 09 | 超长消息 `ERR_TOO_LARGE=4`；恰好 max_msg 边界可发 |
| 10 | 第二个存活生产者被租约阻塞；持有者退出后租约干净移交 |
| 11 | 零长消息（含连续空行）与 max_msg 边界长度 |

输入样例：`examples/input.txt`（含空行、制表符、百分号、中文）。

---

## 7. 文件清单

```
shmring.h / shmring.cpp   核心库（布局/原子/futex/robust 锁/恢复/CRC32）
producer.cpp              生产者驱动（--gen / --stdin / kill 钩子）
consumer.cpp              消费者驱动（--count / 超时 / kill 钩子）
ctl.cpp                   管理：create/destroy/info/doctor/check/damage
cli_common.h              CLI 公共工具与合成消息生成器
Makefile                  构建；make test/demo/check/versions
deps.lock                 make versions 锁定的工具链与依赖（无第三方库）
tests/run_tests.py        11 组真实进程自动化测试
scripts/demo.sh           双进程手工演示
examples/input.txt        示例输入
```

## 8. 已知边界 / 非目标

- Linux 专有（futex、robust mutex）；小端平台（open 时断言）。
- SPSC 模型：同一时刻最多一个存活生产者、一个存活消费者，由租约强制。
- CRC32 用于完整性检测（半写/位翻转），**不是**密码学 MAC。
- 崩溃后消费者语义为至少一次；需要精确一次的上层请以绝对 `seq` 去重。
- 队列满且消费者永久死亡时生产者会按其 `--timeout` 等待或被信号打断；
  不存在静默丢弃路径。
