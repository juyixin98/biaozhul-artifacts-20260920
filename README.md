# ROS 回放断点服务 (ROS Replay Checkpoint Service)

纯后端的 ROS 2 bag 回放服务：按 **bag 内记录时间** 和 **稳定消息序号** 回放本地测试 bag，
支持暂停、变速、跳转；每次跳转产生新的**回放代次 (generation)**，旧代次已排队但未发出的
消息**绝不会**被继续发布。检查点对 bag 摘要、topic 筛选和消息位置做密码学封存，源文件
发生任何变化都会拒绝恢复。

技术栈：Python 3.12 · `rosbag2_py`（ROS 2 Jazzy，mcap 存储）· FastAPI / uvicorn。

---

## 1. 它保证什么

| 需求 | 实现 |
|------|------|
| 按时间回放 | 虚拟时钟 `v(now) = v0 + rate·(wall − t0)`，消息在虚拟时钟到达其**原始 bag 时间戳**时发出 |
| 稳定消息序号 | 建会话时一次性建立全量索引，序号 `1..N` 与 topic 筛选无关，跨暂停/变速/检查点保持稳定 |
| 多 topic 同时间确定序 | 排序键 `(timestamp_ns, topic, sha256(payload))`，不依赖存储插件内部读取顺序 |
| 暂停 / 变速 | 冻结/重锚虚拟时钟；变速只改变后续挂钟等待，**信封里的 `timestamp_ns` 永远是原始 bag 时间戳** |
| 跳转产生新代次 | `seek`/`stop` 使 `generation += 1` 并清空旧代次的有效性 |
| 旧代排队消息不发布 | 调度线程在**同一把锁内**完成「读取代次凭证 → 发布」；被 seek 提前唤醒会重检代次后再决定是否发布，旧代次消息无法到达 sink |
| 检查点 | HMAC-SHA256 签名（canonical JSON）；内容含每个存储文件的大小+SHA-256、全量消息索引哈希、topic 筛选、next_seq |
| 源变化拒绝恢复 | 恢复时重新索引并比对文件清单与消息索引哈希，任一不一致返回 `409 source_changed` |
| 损坏 bag 拒绝 | 打开失败 / 计数不符 / 起始时间不符 / 文件缺失 等返回 `422 corrupt_bag` |
| 路径安全 | bag URI 必须位于 `REPLAY_BAG_ROOTS` 允许根之内，否则 `403 bag_access_denied` |

实测时序（11 条、间隔 100 ms 的 bag）：1x 挂钟耗时 `1.005s`，10x 耗时 `0.101s`，
两种速率下发出去的 `timestamp_ns` 序列完全相同且等于 bag 原始时间。

---

## 2. 目录结构

```
app/
  config.py       环境变量配置（允许根、状态目录、HMAC 密钥、传输方式）
  bagstore.py     bag 校验 / 确定性索引 / 文件+消息双层摘要 / 损坏检测
  crypto.py       HMAC-SHA256 签发与验签、canonical JSON、密钥管理
  checkpoints.py  检查点保存 / 签名验证 / 恢复前源比对
  playback.py     回放引擎：虚拟时钟、代次、暂停/变速/跳转、取消竞争
  sinks.py        LoopbackSink（进程内 NDJSON + 历史）与 RosSink（rclpy raw 发布）
  sessions.py     会话注册表 / 检查点 / 恢复
  models.py       Pydantic 请求模型
  main.py         FastAPI 应用（create_app 工厂 + 路由 + 错误映射）
bagtools/
  bagmaker.py     小型确定性 bag 生成器（含 7 种损坏模式），也是测试夹具
scripts/
  run_server.sh         本地启动
  verify_subscriber.py  订阅验证节点（NDJSON 协议校验 / 真实 DDS 校验）
  run_acceptance.sh     一键端到端验收
tests/                  pytest：42 个用例
examples/
  demo_bag/             示例正常 bag（/alpha /beta /gamma，40 条，含同时间组）
  demo_bag_corrupt/     示例损坏 bag（截断存储）
requirements.txt / requirements.lock
```

---

## 3. 环境准备

需要 Ubuntu + ROS 2 Jazzy（`rosbag2_py`、`rclpy` 通过 apt 随 ROS 发布，**不能 pip 安装**）。
本机已在 `/opt/ros/jazzy` 验证。

```bash
source /opt/ros/jazzy/setup.bash
python3 -m pip install -r requirements.lock   # 仅 PyPI 侧：fastapi/uvicorn/pytest/httpx…
```

未 source ROS 时，纯密码/协议用例仍可运行；所有依赖 `rosbag2_py` 的用例会自动 skip。

---

## 4. 本地启动

```bash
source /opt/ros/jazzy/setup.bash
./scripts/run_server.sh
# 或：python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

关键环境变量（均有默认值）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `REPLAY_BAG_ROOTS` | `examples/` + 当前目录 | 允许访问的 bag 根，`:` 分隔 |
| `REPLAY_STATE_DIR` | `./.replay_state` | 检查点与 HMAC 密钥目录 |
| `REPLAY_HMAC_KEY` | （缺省时生成 256 位随机密钥，0600 落盘） | 检查点签名密钥 |
| `REPLAY_TRANSPORT` | `loopback` | `loopback`（默认，NDJSON）或 `ros`（真实 DDS） |
| `REPLAY_CONTROL_TOPIC` | `/replay/control` | `ros` 传输下发控制标记的 topic |
| `REPLAY_PORT` | `8000` | 监听端口 |

健康检查：

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok","rosbag2_py":true,"transport":"loopback"}
```

---

## 5. HTTP API 速览

| 方法 路径 | 作用 |
|-----------|------|
| `GET  /health` | 健康/ROS 可用性 |
| `GET  /bags` | 列出允许根下的 bag |
| `GET  /bags/inspect?uri=...` | 查看 bag 摘要（话题、计数、索引哈希、文件哈希） |
| `POST /sessions` | 建会话 `{bag_uri, topics?, rate?, autoplay?, transport?}` |
| `GET  /sessions` / `GET /sessions/{id}` | 列表 / 详情 |
| `DELETE /sessions/{id}` | 结束会话 |
| `POST /sessions/{id}/play` · `/pause` · `/stop` | 播放 / 暂停 / 停止（stop 开新代次） |
| `POST /sessions/{id}/rate` | `{rate}` 变速，范围 0.01–100 |
| `POST /sessions/{id}/seek` | `{seq?|timestamp_ns?|ratio?, play_after?}` 三选一，开新代次 |
| `POST /sessions/{id}/filter` | `{topics:[...]}` 或 `null` 清空筛选 |
| `GET  /sessions/{id}/stream` | **NDJSON 实时流**（仅 loopback），每行一个信封 |
| `GET  /sessions/{id}/messages` | 轮询最近信封（有界历史）`?after_history_id=&limit=` |
| `POST /sessions/{id}/checkpoint` | `{pause:true}` 存检查点 |
| `GET  /sessions/{id}/checkpoint/{cp}` | 下载已验签的检查点载荷 |
| `POST /restore` | `{envelope?|checkpoint_id?|checkpoint_path?}` 恢复（源变 → 409） |

### NDJSON 信封

消息（`kind:"message"`）：
```json
{"kind":"message","session_id":"…","generation":2,"generation_seq":7,
 "seq":18,"topic":"/alpha","type":"std_msgs/msg/String",
 "timestamp_ns":1850000000,"data_length":18,
 "data_sha256":"…","data_b64":"<原始 CDR，base64>"}
```
事件：`transport`（playing/paused/stopped）、`seek`、`rate`、`filter`、`generation`、
`end_of_bag`、`restore`，均带 `generation`，订阅者据此判定代次边界。

### 一次完整调用

```bash
BAG="$PWD/examples/demo_bag"
SID=$(curl -s -X POST localhost:8000/sessions -H 'Content-Type: application/json' \
  -d "{\"bag_uri\":\"$BAG\",\"rate\":20}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')

curl -s -X POST localhost:8000/sessions/$SID/seek \
  -H 'Content-Type: application/json' -d '{"seq":1,"play_after":true}'
curl -s -N localhost:8000/sessions/$SID/stream          # 观察信封（Ctrl-C 结束）
curl -s -X POST localhost:8000/sessions/$SID/rate -H 'Content-Type: application/json' -d '{"rate":0.5}'
curl -s -X POST localhost:8000/sessions/$SID/seek -H 'Content-Type: application/json' -d '{"ratio":1.0}'
CP=$(curl -s -X POST localhost:8000/sessions/$SID/checkpoint -H 'Content-Type: application/json' -d '{"pause":true}')
echo "$CP"
# 进程重启后：
curl -s -X POST localhost:8000/restore -H 'Content-Type: application/json' \
  -d "{\"checkpoint_id\":$(echo "$CP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["checkpoint_id"])')}"
```

---

## 6. bag 生成器与订阅验证节点

生成正常 bag：
```bash
source /opt/ros/jazzy/setup.bash
python3 -m bagtools.bagmaker examples/demo_bag \
  --topics /alpha /beta --messages 40 --gap-ms 100 --same-time-groups 3
```
生成损坏 bag（`truncate_storage / garbage_storage / missing_metadata / bad_metadata /
missing_storage / extra_file / payload_tamper`）：
```bash
python3 -m bagtools.bagmaker /tmp/bad --corrupt garbage_storage --messages 12
```

订阅验证节点（独立进程，校验协议不变量并出 JSON 报告）：
```bash
# NDJSON（无需 DDS）：在另一个终端对 SID 做 play/seek，节点检查
#   同代次序号严格递增且稠密、时间戳不倒退、同时间按 topic 排序、
#   seek 之后不得出现旧时间戳消息、至少观察到两个代次
python3 scripts/verify_subscriber.py --mode ndjson --session-id $SID --duration 10

# 真实 DDS：raw 订阅 bag 原始 topic + 控制 topic，验证 seek 后无旧消息
python3 scripts/verify_subscriber.py --mode ros --topics /alpha /beta --bag $BAG --duration 20
```

---

## 7. 自动化测试与一键验收

```bash
source /opt/ros/jazzy/setup.bash
python3 -m pytest -q          # 42 个用例
```

覆盖（对应任务点名的四类场景）：

* **重启续播** — `tests/test_checkpoints.py::test_checkpoint_restarts_and_continues_from_position`
  以及 `test_checkpoint_restart_continues_via_http`：播放中途存点，丢弃全部内存状态（模拟进程重启），
  用签名检查点恢复，剩余消息从 `next_seq` 起**恰好发一次**。
* **末尾跳转** — `test_seek_to_end_then_back_starts_fresh_generation` /
  `test_create_play_seek_stream_full_flow`：跳到 `seq=N+1` 后再跳回头部，新代次完整重放。
* **损坏 bag** — `test_corrupt_bags_rejected[*]`（5 种结构性损坏，索引期 `422`）、
  恢复后损坏/改动（`409 source_changed`）、签名被改（`400 bad_checkpoint_signature`）。
* **取消竞争** — `tests/test_cancel_race.py`：
  * `test_old_generation_messages_never_published[300]` 以高并发随机 seek/stop/变速 持续 ~2.5s，
    断言无旧代消息越界、同代次 seq 连续、时间戳不倒退、seek 目标被遵守；
  * `test_seek_during_wait_race…` 是为一个真实竞态（等待被 seek 提前唤醒）写的确定性回归；
  * `test_stop_then_fast_seek_race_no_old_messages` 反复 stop+seek。
* 另含真实 DDS 集成 `test_ros_transport_republishes_cdr_on_topics`（raw 发布订阅、控制标记）。

一键端到端（真实拉起 uvicorn，含上述全部 + 验证节点 + pytest）：

```bash
source /opt/ros/jazzy/setup.bash
./scripts/run_acceptance.sh
# 期望最后输出：ALL ACCEPTANCE CHECKS PASSED
```

最近一次本机实测：**42 passed**，验收脚本 `ALL ACCEPTANCE CHECKS PASSED`。

---

## 8. 设计要点：为什么旧代次不会漏发

调度线程与控制 API 通过同一把 `threading.Condition` 串行化关键区：

1. 调度线程只在持锁时读取当前 `generation`、计算 `generation_seq` 并调用 `sink.publish_message`，
   发布完成后才推进 `next_seq` / `generation_published`。
2. 一次 `seek`/`stop` 要自增代次，必须先拿到这把锁 —— 因此它不可能插进「读凭证 → 发布」中间。
3. 调度线程为等待下一条消息的截止时间而阻塞时，seek 会 `notify_all` 提前唤醒它；唤醒后
   **重新检查代次/播放态**，若已变化则丢弃本轮旧位置，从循环顶部用新代次重新计算。
4. sink 的历史追加与各订阅者分发也在各自锁内按调用次序编号，事件与消息的先后可由订阅者重放验证。

这四点共同保证：**代次一旦被取代，属于旧代次的任何消息都不会再到达 sink / DDS。**

---

## 9. 局限与如实说明

* 面向「本地小型测试 bag」：建会话时会全量读取并在内存保存每条 payload（按需求回放本地测试 bag）。
  超大 bag 可改为按需从存储按 seq 读取，索引/哈希逻辑不变。
* `ros` 传输下，bag 里消息的类型必须是当前环境可 import 的 ROS 消息类型；示例与测试只用
  `std_msgs/msg/String`。CDR 原样字节通过 raw publisher 发布，不做反序列化改写。
* 本机只装有 mcap 存储插件（无 sqlite3），生成与读取均使用 mcap；存储 ID 从 `metadata.yaml`
  读取，代码路径对其它插件通用，但未在此环境实测。
