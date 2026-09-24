# seglog — 分段追加日志存储服务(崩溃恢复)

纯后端 Rust 服务:分段(segment)追加式日志存储,HTTP 接口基于 axum。
每条记录带**长度、序号、CRC32**,写入路径 `write → fsync → 提交`,
支持在每个写入/同步边界注入崩溃并验证恢复语义。

## 记录格式

每条记录在段文件中的布局(小端,头部固定 20 字节):

```
+-----------+-----------+-----------+-----------+===========+
| magic u32 | len   u32 | seq   u64 | crc   u32 | payload   |
| "SGL1"    | payload长 | 序号      | CRC32     | len 字节  |
+-----------+-----------+-----------+-----------+===========+
```

- CRC32 覆盖 `seq(8 字节 LE) || payload`,序号与内容被篡改都能检出;
- 单条 payload 上限 8 MiB(防止损坏的长度字段触发超大分配);
- 段文件名为 `<16 位十进制段号>.seg`,从 0 连续递增,写满
  `SEGLOG_MAX_SEGMENT`(默认 1 MiB)后滚动;滚动前先 fsync 旧段,
  创建新段后 fsync 目录。

## 持久化与恢复语义

写入路径:**write → fsync(`sync_data`)→ 内存提交(分配序号)→ HTTP 应答**。
即"提交成功前已持久化";只有拿到 `201` 应答的记录才算**已确认**。

打开(恢复)时的规则:

1. 段号必须连续,缺段即数据丢失 → **拒绝打开**;
2. **中段(已封存段)必须完整**:魔数错、长度越界、CRC 错、尾部不完整、
   空段,任何一种都**拒绝打开**,绝不静默跳过;
3. **末段(活动段)仅允许截去末尾的不完整记录**(撕裂写残留),
   截断后 fsync 落盘;完整但 CRC 错误的尾记录同样**拒绝打开**;
4. 全日志序号必须从 1 连续递增,有空洞即拒绝打开;
   恢复后 `next_seq = 最大序号 + 1`,**序号绝不复用**。

恢复后语义:

- **已确认记录**(应答过 201)必须保留 —— 它已 fsync;
- **未确认记录**(崩溃发生在应答前)允许存在或丢失;若存在,内容与序号
  必须完整正确(CRC 校验通过),后续记录接续编号,不复用序号。

## 崩溃注入(仅用于测试/演示)

环境变量 `SEGLOG_CRASH` 在三个边界注入崩溃(触发时进程以退出码 137 直接退出,
模拟 SIGKILL/断电,不运行析构):

| 取值           | 崩溃点                                     |
| -------------- | ------------------------------------------ |
| `after_write`  | 记录已 write(可能仅在页缓存),尚未 fsync |
| `after_sync`   | 已 fsync 持久化,尚未在内存中提交          |
| `after_commit` | 已提交(序号已分配),HTTP 应答发出之前    |

段滚动路径上的 write/fsync 边界由同一注入机制覆盖(滚动发生在 append 内)。

## 构建与运行

依赖:Rust 工具链(开发用 1.98.1); crates 见 `Cargo.toml`,版本由
`Cargo.lock` 锁定(axum 0.8、tokio 1、crc32fast 1、serde 1)。

```bash
cargo build            # 构建(调试版)
cargo test             # 运行全部自动化测试(含崩溃注入端到端测试)

# 启动服务
SEGLOG_ADDR=127.0.0.1:3000 \
SEGLOG_DATA_DIR=./data \
SEGLOG_MAX_SEGMENT=1048576 \
./target/debug/seglog

# 一键演示(读写 → 滚动 → 崩溃注入 → 恢复 → 损坏拒绝启动)
./examples/demo.sh
```

配置项(均为环境变量):

| 变量                 | 默认值             | 说明                     |
| -------------------- | ------------------ | ------------------------ |
| `SEGLOG_ADDR`        | `127.0.0.1:3000`   | 监听地址                 |
| `SEGLOG_DATA_DIR`    | `./data`           | 数据根目录(每个日志一个子目录) |
| `SEGLOG_MAX_SEGMENT` | `1048576`          | 单段大小上限(字节)     |
| `SEGLOG_CRASH`       | 无                 | 崩溃注入点(见上表)     |

## HTTP 接口

| 方法 | 路径                          | 说明                                   |
| ---- | ----------------------------- | -------------------------------------- |
| POST | `/logs/{name}/records`        | 追加记录,body `{"payload":"..."}`,返回 201 `{"seq":N,"segment":I}` |
| GET  | `/logs/{name}/records`        | 列出全部记录 `[{"seq","len"}]`         |
| GET  | `/logs/{name}/records/{seq}`  | 按序号读取 `{"seq","payload"}`         |
| GET  | `/logs/{name}/status`         | 状态:`next_seq`、记录数、段列表        |
| POST | `/logs/{name}/roll`           | 手动滚动段(空活动段不滚动)             |
| GET  | `/healthz`                    | 健康检查                               |

日志名:`[A-Za-z0-9_-]{1,64}`。payload 目前为 UTF-8 文本(JSON 字符串)。

### 请求样例

```bash
# 追加(201 即已确认:此前已完成 write+fsync)
curl -X POST localhost:3000/logs/events/records \
  -H 'Content-Type: application/json' -d '{"payload":"user-login"}'
# => {"seq":1,"segment":0}

curl localhost:3000/logs/events/records/2     # => {"payload":"add-to-cart","seq":2}
curl localhost:3000/logs/events/records       # => {"records":[{"len":10,"seq":1},...]}
curl localhost:3000/logs/events/status        # => {"active_segment":0,"next_seq":4,...}
curl -X POST localhost:3000/logs/events/roll  # => {"active_segment":1}
```

## 自动化测试(验收)

`tests/crash_recovery.rs` 以子进程启动真实服务做端到端验证:

| 测试                                   | 验证点                                                   |
| -------------------------------------- | -------------------------------------------------------- |
| `basic_append_read_list_status`        | 基本读写、序号从 1 连续、状态查询、404                    |
| `segment_roll_and_restart_recovery`    | 段滚动、重启恢复、序号接续不复用                          |
| `crash_after_write_boundary`           | write 后/fsync 前崩溃:已确认记录保留,未确认记录允许存在或丢失,序号不复用 |
| `crash_after_sync_boundary`            | fsync 后/提交前崩溃:同上                                |
| `crash_after_commit_boundary`          | 提交后/应答前崩溃:记录已持久化,序号不复用               |
| `repeated_crash_recovery_loop`         | 6 轮反复崩溃-恢复 + 小段强制滚动:已确认记录逐条核对,序号始终连续 |
| `incomplete_tail_record_truncated`     | 末段不完整尾记录被截去,前序记录保留,新记录接续编号      |
| `corrupt_middle_segment_rejected`      | 中段翻转 1 字节 → 拒绝启动(非零退出),不静默跳过        |
| `corrupt_tail_record_crc_rejected`     | 末段完整记录 CRC 损坏 → 拒绝启动                          |
| `corrupt_magic_rejected`               | 魔数损坏 → 拒绝启动                                       |

## 实测结果

在本机(Linux 6.8,rustc 1.98.1)实际执行:

```
$ cargo test
running 10 tests
test basic_append_read_list_status ... ok
test corrupt_magic_rejected ... ok
test corrupt_tail_record_crc_rejected ... ok
test incomplete_tail_record_truncated ... ok
test corrupt_middle_segment_rejected ... ok
test segment_roll_and_restart_recovery ... ok
test crash_after_commit_boundary ... ok
test crash_after_sync_boundary ... ok
test crash_after_write_boundary ... ok
test repeated_crash_recovery_loop ... ok

test result: ok. 10 passed; 0 failed; 0 ignored
```

`./examples/demo.sh` 实测输出(节选):

```
=== 5. 崩溃注入: SEGLOG_CRASH=after_commit ===
  (curl 退出码 52 —— 应答丢失)
服务退出码: 137 (137 = 崩溃注入)

=== 6. 重启恢复: 未确认记录允许存在,序号不复用 ===
{"records":[...,{"len":12,"seq":9}]}        # seq=9 为崩溃时未确认记录,已持久化
{"seq":10,"segment":1}                       # 新记录接续编号

=== 7. 中段损坏: 拒绝启动 ===
[seglog] 打开日志 "events" 失败: 中段 0000000000000000.seg 损坏,拒绝打开: 偏移 30: 魔数错误 0x53474cce
服务退出码: 1 (非零 = 拒绝启动)
```

## 已知限制 / 未完成项

- **崩溃模拟的局限**:注入方式为进程内 `exit(137)`,页缓存数据在"崩溃"后
  仍然保留,因此 `after_write` 场景下未确认记录在测试中总是存在(真实断电
  可能丢失)——两种结果都符合语义,但未做真实断电/`echo c > sysrq` 级验证。
- 读取路径(`GET`)每次实时解析段文件,O(段大小),未建索引;数据量大时较慢。
- 写路径在 async handler 内直接做同步文件 I/O(未用 `spawn_blocking`),
  高并发下会阻塞 tokio worker;本服务定位为小规模/演示用途。
- payload 仅支持 UTF-8 文本,无二进制(base64)接口。
- 无删除/压缩(compaction)、无保留策略、无认证与 TLS。
- 恢复时发现损坏即整体拒绝启动,未提供"丢弃损坏日志、保留其余日志"的
  降级模式(需人工介入处理损坏数据)。
