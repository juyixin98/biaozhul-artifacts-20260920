# shmrq — POSIX 共享内存 SPSC 环形队列

单生产者 / 单消费者、固定容量、变长消息上限的跨进程无锁环形队列（C++17 /
POSIX `shm_open` / Linux futex）。纯后端，无前端。

- 容量：2..65536 个槽，必须是 2 的幂（默认 64）
- 单条消息：0..`max_payload` 字节（默认 4000，硬上限 2^24−1），变长
- 序号：**单调 64 位位置计数**，环索引 = `pos & (capacity-1)`；回绕有专门计数器，且 2^64 空间耗尽显式报错
- 满队列策略：非阻塞拒绝（退出码 5）或 CLOCK_MONOTONIC 绝对超时阻塞（futex）
- 崩溃语义：生产者死在半写槽 → 重启识别并回收，**不丢已确认消息**；
  已确认数据 CRC 损坏 → 拒绝服务并要求显式重新初始化，绝不静默丢弃

## 目录

```
include/shmrq/ring.h   ABI：Header / SlotHdr / Ring 接口
include/shmrq/shm.h    POSIX shm + OFD 字节区间锁
include/shmrq/futex.h  进程共享 futex 封装
src/                   实现 + CLI（shmrq 可执行文件）
tests/unit.cpp         单元/双进程测试（fork + MAP_SHARED）
tests/run_tests.sh     12 组真实双进程集成夹具
examples/messages.txt  示例输入
examples/demo.sh       手工演示（含杀生产者）
DEPS.md                依赖与版本锁定
```

## 依赖（已锁定，见 DEPS.md）

- Linux ≥ 4.0（OFD 锁 `F_OFD_SETLK`、futex；实测 Ubuntu 24.04 / 内核 6.8）
- g++ ≥ 10（C++17；本仓用 13.3.0）或 clang ≥ 12
- cmake ≥ 3.10、GNU make、bash ≥ 4、python3（仅夹具生成定长行）
- 运行库：glibc（`librt`、`libpthread`），无第三方库

## 构建与本地启动

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure   # 单元 + 集成
# 或分步：
./build/shmrq_unit
bash tests/run_tests.sh ./build/shmrq
```

手工体验：

```bash
Q=/demo
./build/shmrq unlink "$Q" 2>/dev/null
./build/shmrq create "$Q" -c 8 -m 4000
./build/shmrq consumer "$Q" -n 3 -t 5000 &        # 终端 A：阻塞等 3 条
./build/shmrq producer "$Q" -n 3 -s 1 -r 200      # 终端 B
./build/shmrq info "$Q"
./build/shmrq unlink "$Q"

# 文件输入（每行一条）
./build/shmrq create "$Q" -c 8 -m 128
./build/shmrq consumer "$Q" -n 5 -t 3000 --no-verify &
./build/shmrq producer "$Q" -f examples/messages.txt

# 崩溃恢复演示（生产者在 seq=3 的半写状态被 SIGKILL，再重启）
bash examples/demo.sh kill
```

## 验收命令（逐条对应需求）

| 需求 | 命令 | 通过判据 |
|---|---|---|
| 双真实进程、高频回绕 | 夹具 2 | cap=8 跑 200000 条（25000 圈），序号 0..199999 稠密无失，wraps=25000 |
| 变长消息上限 | 夹具 3 | 1..3980 随机变长 + 恰好 4096 帧成功，超长行被跳过且队列完好 |
| 满队列拒绝/超时 | 夹具 4、12 | 非阻塞退出 5；阻塞者被 futex 真实挂起，消费者腾位后自动继续 |
| 半写槽识别与恢复 | 夹具 5 | `info` 可见 `pending_write=1`；SIGKILL 后重启日志 `adopted=1`；500 条已确认消息一条不丢，新消息从位置 500 连续 |
| 随机时刻终止并重启 | 夹具 6 | 生产者被 SIGKILL 两次后重启，已投递序号无重复、达 19999 |
| 消费者崩溃 | 夹具 7 | 未确认消息重新投递（at-least-once），全部 CRC 合法 |
| 单生产者/单消费者互斥 | 夹具 8 | 存活进程持锁，第二个生产者/消费者退出 2；旧进程被 kill 后内核自动放锁 |
| 数据损坏不静默丢失 | 夹具 9 | 消费者退出 3，生产者退出 7（REINIT），`unlink`+`create` 后恢复 |
| 示例输入 | 夹具 10 | `examples/messages.txt` 5 行全部送达 |
| 进程 vs 线程区分 | `shmrq_unit` | 线程用例仅做冒烟；fork 双进程用例（MAP_SHARED 同物理页、不同页表）验证进程共享原子/futex |

退出码：`0` ok，`2` 锁冲突，`3` 完整性失败，`4` 空闲超时，
`5` 满队列超时/拒绝，`6` 用法错误，`7` 必须重新初始化。

## 协议与内存序（消费者不可能读到未提交内容）

每个槽 24 字节定长头：

```
atomic<u64> seq;   // Vyukov 序号：唯一发布点
atomic<u32> tag;   // 0 / WRITING(0x57524954)
atomic<u32> len;   // 记录长度（原子，杜绝跨进程撕裂）
atomic<u32> crc;   // CRC32(LE(seq)||LE(len)||payload)
```

`head`/`tail` 是**单调位置**（已发布/已消费的记录数），缓存行隔离；
槽 i 初始 `seq=i`。

**发布（生产者）两阶段：**

1. `reserve(pos)`：acquire 读槽 `seq`：
   - `seq == pos`（dif=0）→ 槽空闲，relaxed 置 `tag=WRITING`（局部占位标记），返回载荷指针；
   - `seq == pos+1-cap`（dif<0）→ 上一轮记录未消费，队列满 → 拒绝或 futex 等消费者；
   - 其它 → `ReinitRequired`。
2. 调用方写载荷；`commit(pos,len,crc)`：relaxed 写 len/crc → **release 栅栏** →
   relaxed 清 `tag` → **release 存储 `seq=pos+1`**。

**读取（消费者）：** acquire 读 `seq`，仅当 `seq==pos+1` 才认为记录存在；
该 acquire 与提交的 release 同步（synchronizes-with），保证载荷、len、crc、
tag 清零全部对消费者可见，随后才可能消费——**未提交槽的 seq 不等于 pos+1，
消费者在协议上无法读到其载荷**。

**释放（消费者，Vyukov 所有权序，不能颠倒）：**

1. release 置槽 `seq=pos+cap`（生产者下一周期等待的"空闲值"）——先把槽
   所有权交还；
2. 再 release 持久化账本 `tail=pos+1`。

顺序绝不能反过来：先记账会让生产者在消费者尚未放槽时复用槽，破坏槽所有权
（早期版本曾如此实现并在两进程压力测试中检出 `ReinitRequired`，已修正）。
头部 `head`/`tail` 字只是**唤醒提示**，权威状态永远是槽序号；因此读到
`head<tail` 等瞬时滞后不会被判为损坏。

环形索引回绕是平凡的掩码运算；**位置自身**的回绕由 64 位单调计数 +
`wraps_head/wraps_tail` 计数器观测（200k@cap8 ⇒ 25000）。若 2^64 位置空间
真的耗尽（每纳秒 10 亿条也要 ~584 年），`reserve` 显式返回 `ReinitRequired`。

阻塞用**进程共享 futex**（无 `FUTEX_PRIVATE_FLAG`，`FUTEX_WAIT_BITSET` 绝对
CLOCK_MONOTONIC 超时）：入睡前重读位置变量，内核原子比对"观察值"，丢失唤醒
在协议上不可能；醒来后重新检查 head/tail，futex 只是优化。

## 崩溃恢复（不静默丢失已确认消息）

权威状态是**每个槽的 seq**，头部 head/tail 只是可能滞后的提示。槽序号对
"位置 + 状态"是无歧义编码（槽 i 初始 `seq=i`；位置 p 提交后为 `p+1`；消费后
为 `p+cap`；被下一周期复用时为 `p+cap+1`）。生产者打开段时执行槽驱动恢复：

1. **跳过已释放/已复用前缀**：从 tail 提示起，凡 `seq==p+cap`（消费者放了槽
   但死在 tail 记账前）或 `seq==p+1+cap`（槽已被生产者复用）的位置，都是
   历史，安全跳过。
2. **前推并校验连续已提交区**：从真实 tail 起逐个消费 `seq==p+1` 的槽，
   **每条都做 CRC 校验**；任何一条不符 → `ReinitRequired`，要求
   `unlink`+`create` 显式重初始化（防止静默丢确认消息）。
3. **回收半写槽**：前沿槽 `seq==head` 且 `tag==WRITING`（或 tag 已清但 seq
   未推进）→ 该记录从未发布，安全回收，计数 `recovered_slots`。
4. 提示合理性检查：真实 head/tail 只会领先于提示，不会落后；否则判损坏。

消费者崩溃的三种时刻：

- 死在 `release()` 之前 → 记录重投（**at-least-once**，CLI 用帧内序号检测并
  在退出码中报告重复数，CRC 仍逐条校验）；
- 死在"放槽之后、tail 记账之前" → 槽已释放，记录既不会重投也不会损坏：
  重启消费者以槽序号为准跳过这些历史位置，从第一个仍 `seq==p+1` 的槽继续；
- 记账之后 → 已确认，不再投递。

生产者恢复只在**本地**采用推进后的 tail（绝不写 tail 字——那是消费者角色的
字，可能有存活消费者并发推进）。

**CRC**：CRC-32/ISO-HDLC（IEEE 802.3），已知答案
`crc32("123456789")=0xCBF43926`（单元测试断言）。域包含 seq+len，换到别的
槽即校验失败。它检测位腐烂/撕裂，**不是密码学认证**（无密钥，不防恶意写入；
段以 0600 权限创建）。

## 进程互斥（OFD 锁，区别于线程假设）

进程"存活"无法靠 PID 判断（PID 会复用）。每个 shm fd 上两把
**open-file-description 字节锁**（偏移 0=生产者，偏移 1=消费者）：

- 锁属于打开的文件描述、由内核持有；进程被 SIGKILL 时内核**保证自动释放**——
  这正是夹具 5/8 验证的"旧生产者死了，新生产者才能接管"；
- 非阻塞获取，冲突返回退出码 2；
- 全部同步原语（原子 + futex）都在 `MAP_SHARED` 内存上，原子类型编译期
  `is_always_lock_free` 检查；线程内冒烟测试**不能**替代双进程验证——
  不同进程的页表映射同一物理页才是真正的进程共享路径，本项目两套测试都有。

## 局限与边界

- SPSC：不支持多生产者/多消费者（由 OFD 门强制）。
- at-least-once：消费者在确认窗口内崩溃可能重投；需要去重请按帧内 20 位序号。
- 消息上限与容量在创建时固定；改参数需 `unlink` 后重建（显式操作，不自动迁移）。
- 段名生命周期：`unlink` 后仍持有映射的进程可继续使用，最后一个映射关闭后回收。
- 仅 Linux（OFD/futex 行为依赖）；无网络/多机语义。
