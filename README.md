# P048 — ROS 2 多机器人命名隔离命令网关

纯后端：**Python + rclpy (ROS 2 Jazzy) + FastAPI**。一个 HTTP 命令网关把
经过 HMAC 签名的测试命令，按「机器人 ID → 独立 namespace」映射，发布到各自
隔离的 ROS 2 topic；并用**映射版本（epoch）**保证命名映射一旦变更，旧授权
立即在链路上失效。配套两台**合成机器人节点**做真实验收。

所有计算、协议、密码操作都是**真实执行**（真 DDS 收发、真 HMAC-SHA256、真
防重放、真有效期判定），没有 mock。

---

## 1. 它解决什么问题 / 保证了什么

| 需求 | 实现 |
| --- | --- |
| 机器人 ID → 独立 namespace | 每个登记机器人一个 **namespace 节点**，相对 topic 由 rclpy 展开为 `/p48/<id>/cmd` 等 |
| 只能向**登记**的 namespace 发布 | 发布者只在登记机器人的节点上创建；外部请求永远不提供 topic |
| topic 禁止绝对路径与跨空间逃逸 | `topics.py` 严格校验：拒绝 `/x`、`~/x`、`..`、空段；FQTN 经固定允许列表拼接；并拒绝 namespace 嵌套 |
| 命令含**目标、序号、有效期、测试身份** | 命令信封字段 `target / seq / expires_at / tester`，全部签名 |
| 同目标序号**不回退** | 网关入口（409 `stale_seq`）+ 机器人端（`stale_seq_at_robot`）双重判定，按 `(robot,target)` 独立计数 |
| 过期命令丢弃**且记录原因** | 入口 `expired_at_ingress`；到达机器人时 `expired_on_arrival`；全部进 per-robot 事件审计 |
| 映射变更后旧授权**立即失效** | 映射变更 → epoch 单调 +1、签名后锁存广播；机器人只接受当前 epoch 的信封（`stale_epoch`）；epoch 持久化防重启回退 |
| 两台合成机器人验证 | 单播 / 重连锁存去重 / 同名 topic / 越权 / 查询与事件订阅隔离，全部自动化 |
| 查询与事件订阅隔离 | status/events/SSE 都带 tester 授权，只能看被授权机器人；SSE 服务端按 robot_id 过滤 |

---

## 2. 架构

```
                HTTP (HMAC 签名 + nonce 防重放)
  tester ─────────────────────────────────────────────► FastAPI (:8088)
   │                                                    │ dispatch_command
   │  X-Tester / X-Timestamp / X-Nonce / X-Signature    ▼
   │                                        ┌──────────────────────┐
   │                                        │ GatewayRuntime (rclpy)│
   │                                        │  每机器人一个 ns 节点  │
   │                                        └──────────┬───────────┘
   │                 TRANSIENT_LOCAL (latched), 相对 topic
   │            ┌──────────────────────┬────────────────┴───────────────┐
   ▼            ▼                      ▼                                ▼
 /p48/alpha/cmd   /p48/alpha/epoch   /p48/bravo/cmd            /p48/bravo/epoch
   ▲                                   ▲
   │  订阅（只在自己的 namespace）       │
┌──┴───────────────┐          ┌────────────────┐
│ synthetic alpha  │          │ synthetic bravo │
│ 验签/epoch/序号/  │          │ 同左             │
│ 有效期/去重       │          └────────────────┘
└──┬───────────────┘
   │ status(心跳) / events(审计)  VOLATILE，回传网关 → 查询 API / SSE
```

- **密码学（真实）**：HMAC-SHA256，密钥为 32 字节 urlsafe-base64。
  - HTTP 请求对 `(method, path, tester, nonce, timestamp, sha256(body))` 的
    规范化 JSON 签名；nonce 在时间窗内去重，时间戳限窗（默认 ±60s）。
  - 命令**端到端信封**用**该 tester 的密钥**签名，机器人独立验签；
    信封还绑定 `robot_id` 与 `namespace`，即使被搬到别处也会被拒
    （`robot_mismatch` / `namespace_mismatch`）。
  - epoch 消息用独立 `epoch_key` 签名。
- **映射版本 epoch**：`机器人ID→namespace` 映射的指纹。仅当映射真正变化时
  （重新加载配置后比较指纹）epoch +1，并持久化到 `<registry>.epoch`，
  重启取 `max(持久值, …)`，无法回退。旧 epoch 的命令信封在机器人处被
  真实验签流程以 `stale_epoch` 丢弃——即「旧授权立即失效」。
- **QoS**：`cmd`/`epoch` 用 RELIABLE + TRANSIENT_LOCAL（锁存），晚加入/重连
  的机器人立刻收到当前 epoch 与最近一条命令；`status`/`events` 用 VOLATILE。

---

## 3. 目录结构

```
.
├── src/p48_gateway/
│   ├── topics.py          # 命名/topic 严格校验（防绝对路径与逃逸）
│   ├── crypto.py          # HMAC-SHA256、规范化 JSON、nonce 防重放
│   ├── protocol.py        # 签名命令信封 / epoch 消息 / 纯验签逻辑
│   ├── qos.py             # latched / volatile QoS
│   ├── registry.py        # 机器人/tester 配置、ACL、epoch 持久化
│   ├── events.py          # 事件总线 + per-robot 审计历史
│   ├── gateway_runtime.py # 真实 rclpy：ns 节点、发布、序号判定
│   ├── robot_runtime.py   # 合成机器人：真实订阅/验签/序号/有效期/去重
│   ├── api.py             # FastAPI：鉴权、命令、查询、SSE、admin reload
│   ├── client.py          # 签名 HTTP 客户端（示例与测试使用）
│   └── __main__.py        # python -m p48_gateway.{gateway,robot}
├── config/registry.example.json
├── examples/              # 示例输入 + 生成签名 curl 的脚本
├── scripts/               # send_cmd.py / demo.sh / gen_keys.py
├── tests/unit/            # 62 个纯逻辑测试（无需 ROS）
├── tests/integration/     # 8 个真 DDS + 真 HTTP 端到端测试
├── requirements.lock.txt  # 锁定依赖
└── setup_env.sh
```

---

## 4. 环境要求与安装

- Ubuntu + **ROS 2 Jazzy**（提供 `rclpy`、`std_msgs`；apt 安装，非 pip 包）。
- Python 3.12。
- 依赖已在本机验证存在，锁定版本见 `requirements.lock.txt`
  （`fastapi 0.141.1 / uvicorn 0.53.0 / httpx 0.28.1 / pydantic 2.13.5 / pytest 9.1.1` 等）。

```bash
# 若需要补装 pip 侧依赖（本机已具备）：
python3 -m pip install --user -r requirements.lock.txt
```

ROS 2 与 PYTHONPATH 用一个脚本统一 source（已验证）：

```bash
source ./setup_env.sh        # source /opt/ros/jazzy + 设置 ROS_DOMAIN_ID=42 + PYTHONPATH=src
```

> 注：本机存在 `HTTP(S)_PROXY/ALL_PROXY` 等代理变量。客户端已用
> `trust_env=False`、脚本回环地址不走代理；如你在 curl 遇到代理问题，
> 设置 `NO_PROXY=127.0.0.1,localhost`。

---

## 5. 本地启动

需要 **3 个终端**（都先 `source ./setup_env.sh`）：

```bash
# 终端 1：网关（默认 127.0.0.1:8088）
python3 -m p48_gateway.gateway --registry config/registry.example.json --port 8088

# 终端 2、3：两台合成机器人（各自进入登记 namespace）
python3 -m p48_gateway.robot --registry config/registry.example.json --robot alpha
python3 -m p48_gateway.robot --registry config/registry.example.json --robot bravo
```

健康检查（无需签名）：

```bash
curl -s http://127.0.0.1:8088/healthz
# {"ok":true,"epoch":1}
```

**一条命令全自动起齐整套 + 跑一遍验收场景**（起 1 网关 + 2 机器人，结束自动清理）：

```bash
P48_PORT=8095 ./scripts/demo.sh
```

---

## 6. 发送命令（示例输入）

请求必须带签名头。用自带的签名客户端：

```bash
# 合法单播到 alpha（202，topic=/p48/alpha/cmd，真实执行）
python3 scripts/send_cmd.py command alpha tester_alpha dock 1 '{"pose":[1,2,0]}'

# 同目标序号回退（再次 seq=1）→ 409 stale_seq
python3 scripts/send_cmd.py command alpha tester_alpha dock 1 '{"pose":[1,2,0]}'

# 越权：tester_bravo 只能操作 bravo，打 alpha → 403
python3 scripts/send_cmd.py command alpha tester_bravo dock 1 '{}'

# 极短有效期：入口或到达时丢弃（记录 expired_* 原因，绝不执行）
python3 scripts/send_cmd.py command alpha tester_alpha dock 99 '{}' 0.2

# 同名 topic、另一 namespace：→ /p48/bravo/cmd
python3 scripts/send_cmd.py command bravo tester_alpha dock 1 '{"speed":0.4}'
```

用示例 JSON 输入，并生成等价的**签名 curl**（便于用纯 curl 复现）：

```bash
python3 examples/sign_curl.py alpha tester_alpha examples/command_valid.json
# 输出一条可直接粘贴的 curl，以及被签名的规范化载荷
```

查询与事件订阅（隔离）：

```bash
python3 scripts/send_cmd.py status alpha tester_alpha   # 心跳/在线/已执行数
python3 scripts/send_cmd.py events alpha tester_alpha   # 仅 alpha 的审计事件
python3 scripts/send_cmd.py stream alpha tester_alpha   # SSE，约 15s，服务端只推 alpha
# tester_bravo 读 alpha → 403
python3 scripts/send_cmd.py events alpha tester_bravo
```

### HTTP 接口

| 方法与路径 | 鉴权 | 说明 |
| --- | --- | --- |
| `GET /healthz` | 无 | 存活与当前 epoch |
| `POST /robots/{id}/commands` | tester | 下发命令；body `{target, seq, command, ttl_seconds?}` |
| `GET /robots` | 无（仅清单） | epoch + 机器人/namespace 清单 |
| `GET /robots/{id}/status` | tester（须含该机器人 ACL） | 在线、epoch、已执行数 |
| `GET /robots/{id}/events?limit=` | tester（同 ACL） | 该机器人审计历史 |
| `GET /robots/{id}/events/stream` | tester（同 ACL） | SSE，服务端按 robot_id 过滤 |
| `POST /admin/registry/reload` | `X-Admin-Key` | 热加载；映射变化才 bump epoch |

签名头：`X-Tester`、`X-Timestamp`（epoch 秒）、`X-Nonce`、`X-Signature`。

---

## 7. 映射变更 → 旧授权立即失效（操作演示）

`config/registry.example.json` 是普通 JSON。修改任一机器人的 namespace
（或增删机器人使 ID→namespace 指纹变化）后热加载：

```bash
curl -s -X POST http://127.0.0.1:8088/admin/registry/reload \
  -H "X-Admin-Key: demo-admin-key-change-me-0123456789"
# {"changed": true, "epoch": 2, ...}
```

- epoch 立刻 +1 并签名锁存广播；机器人采用后只接受 epoch=2 的新信封。
- 任何携带旧 epoch 的旧信封（即使签名密钥仍有效）在机器人处被
  `stale_epoch` 丢弃；新命令发布到**新 namespace**的 topic。
- 未改变映射的重复 reload **不会** bump epoch（测试 `test_06` 有断言）。
- epoch 持久化于 `config/registry.example.json.epoch`（已 gitignore），
  重启不会回退。

> 生产使用请用 `python3 scripts/gen_keys.py <path>` 重新生成全部随机密钥。
> 示例仓库里的密钥仅用于本地演示。

---

## 8. 验收命令（自动化测试）

```bash
source ./setup_env.sh

# 纯逻辑单测（62）：命名/逃逸、HMAC、防重放、信封验签、注册表
python3 -m pytest tests/unit -q

# 端到端集成测试（8，真 uvicorn + 真 rclpy/DDS + 两台合成机器人）
python3 -m pytest tests/integration -q

# 全量
python3 -m pytest -q
```

实测结果（本机，ROS 2 Jazzy，`ROS_DOMAIN_ID=42`）：

```
70 passed in ~8s
```

集成测试覆盖（见 `tests/integration/test_gateway_flow.py`）：

1. `test_01_unicast_one_robot_only` — 单播只到目标机器人
2. `test_02_same_named_topic_isolation` — 同名 topic `cmd` 在另一 namespace 互不可达
3. `test_03_seq_monotonic_gateway_and_robot` — 同目标序号不回退（入口 409，目标独立计数）
4. `test_04_ttl_expiry_paths` — 入口/到达两处真实过期丢弃 + 审计原因
5. `test_05_unauthorized_requests` — 篡改签名、重放 nonce、ACL、空授权访客、未登记机器人、缺头
6. `test_06_mapping_change_invalidates_old_auth` — reload 改 namespace → epoch 递增、新命令走新 topic、旧 epoch 信封 `stale_epoch`
7. `test_07_reconnect_latched_and_dedup` — 断链重连：锁存重投被去重、后续高序号照常
8. `test_08_query_and_sse_isolation` — status/events/SSE 跨机器人隔离

---

## 9. 安全/边界说明（如实）

- 示例 `admin_key` 与 tester 密钥是**演示密钥**，且示例配置随仓库分发；
  生产必须 `gen_keys.py` 重生成并妥善保管。
- 传输为明文 HTTP，仅监听 `127.0.0.1`；跨机部署请置于 TLS/内网之后。
- 事件与状态保存在内存（有界历史，默认 100 条/机器人）；进程重启历史清空，
  但 epoch 通过 sidecar 持久保持单调。
- 时间戳防重放窗口依赖时钟基本同步（默认 ±60s）。
- SSE 为约 15s 的短连接演示实现；需要长连可在 `api.py` 调整预算/改成长轮询。
