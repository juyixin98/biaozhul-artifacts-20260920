# 机器人实验快照服务 (robot-experiment-snapshot)

机器人**离线实验**的可复现快照服务（纯后端）。把 **bag 摘要、运行参数、坐标标定、算法版本**
四者绑定成一个**不可变实验（snapshot）**；作业只读取固定快照；产物先校验再发布索引；
重复执行按内容记录为不同尝试，证据永不覆盖。

技术栈：Python 3.12 · FastAPI · SQLite（标准库 sqlite3 + 触发器）· 标准库 `hashlib`/`hmac`
完成全部密码学操作，无外部密码学依赖。

---

## 1. 它如何保证“可复现”

### 1.1 不可变快照

四类输入各自以**规范 JSON（键排序、紧凑、UTF-8）的 SHA-256** 为内容标识：

| 对象 | ID 形式 | 说明 |
|---|---|---|
| bag | `bag-<sha256(原始字节)>` | 上传后服务端真实解析，落盘内容寻址证据并生成摘要 |
| 参数 | `params-<sha256>` | 抽样率、网格大小、工作域等 |
| 坐标标定 | `cal-<sha256>` | 2×2 矩阵 + 平移，真实参与坐标变换 |
| 算法 | `algo-<sha256>` | 名称 + 版本 |

快照 ID = `snap-<sha256(四者引用的规范JSON)>`。快照一旦创建：

- 协议层**没有也不允许** PUT/PATCH/DELETE（返回 405）；
- 数据库层 `bags/params/calibrations/algorithms/snapshots/runs/artifacts/index_entries`
  全部带 `BEFORE UPDATE/DELETE ... RAISE(ABORT)` 触发器，绕过 API 直接改库也会被 SQLite 拒绝；
- **发布新标定只是新增一条内容**，旧快照引用的标定哈希不变 → 作业结果不变；要用新标定须显式建新快照。

### 1.2 确定性计算（真实执行，不是打桩）

流水线见 `app/compute.py`：

1. 读 bag 检测点 → 2. 应用 2×2 标定矩阵与平移 → 3. 工作域过滤 →
4. **运行种子驱动 SplitMix64** 确定性抽样（`app/prng.py`，不依赖全局 `random`）→
5. 网格 4-邻域并查集聚类 → 6. 输出簇质心/点列表，浮点量化 6 位。

相同（快照输入，种子）在任何机器、任何 Python 版本上产出**逐字节相同**的规范结果；
改种子或任一输入，结果必然不同。

### 1.3 先校验，再发布索引

每次运行（`run` = 快照 + 种子，幂等）下的一次执行是一次**尝试（attempt）**，状态机：

```
running ──成功──▶ succeeded ──显式标记──▶ marked_reproducible
   │
   ├──输入证据缺失/被替换──▶ failed(input_verification_failed)
   ├──产物重算不一致────────▶ failed(output_verification_failed)
   └──产物缺失──────────────▶ failed(artifact_missing)
进程在 running 时崩溃，服务重启后 ──▶ interrupted(interrupted_on_restart)
```

成功路径上，服务在发布前做的事（`app/service.py`）：

1. 从内容寻址证据库读回 bag 字节，**重新哈希并解析**，与快照记录比对；
2. 真实计算结果；
3. 用相同输入与种子**独立重算一遍**，与实际输出做**逐字节**比较（防运行期被改）；
4. 全部通过才：写产物证据 → 同一数据库事务内写**索引**（内含四个输入哈希、种子、产物哈希）
   并用 HMAC-SHA256 签名（密钥首次启动真实生成 256 位随机值，存 `data/hmac.key`，权限 0600）→
   状态置 `succeeded`。

任一步失败：**绝不发布索引**，尝试置 `failed`，错误原因也作为内容寻址证据保存。

### 1.4 缺一项就不能标为可复现

`POST /attempts/{id}/mark-reproducible` 先跑 8 项独立校验，缺一即 **409**：

`attempt_succeeded` · `seed_recorded` · `artifact_present` · `artifact_bytes_valid` ·
`index_signature_valid`(HMAC) · `index_payload_consistent` ·
`recomputation_matches`(独立重算逐字节一致) · `result_binds_snapshot_inputs`。

产物文件本身记录了四个输入哈希与种子（见结果 JSON 的 `inputs`/`seed` 字段）。

### 1.5 证据按内容寻址，重复执行不覆盖

- 所有证据（bag、产物、错误）存于 `data/evidence/<前2字符>/<完整sha256>`；
- 相同内容 → 同一路径（去重、复用）；不同内容 → 不同路径（**物理上不可能覆盖**）；
- 同一 run 反复执行 → attempt_no 递增的多条尝试记录；成功尝试可共享同一产物哈希，失败尝试各自留痕；
- 磁盘证据被替换/删除时：运行前校验与 `/verify` 都会如实报错；
  只能用**与哈希一致的原内容**经 `PUT /evidence/{digest}` 恢复（哈希不符一律 422，不能借修复篡改身份）。

---

## 2. 目录结构

```
app/
  config.py      # 数据目录 (SNAPSHOT_HOME, 默认 ./data)
  canonical.py   # 规范化 JSON 序列化
  crypto.py      # SHA-256 / HMAC-SHA256 / 常量时间比较 / 密钥生成
  prng.py        # SplitMix64 确定性伪随机数
  compute.py     # 真实离线实验: 标定→过滤→种子抽样→聚类
  storage.py     # 内容寻址证据库 (不覆盖, 可按哈希修复)
  db.py          # SQLite schema + 不可变触发器
  models.py      # Pydantic 请求模型
  service.py     # 业务逻辑/状态机/崩溃恢复/校验发布
  main.py        # FastAPI 路由
examples/        # 小型合成数据: bag、参数、两版标定、两版算法
demo/run_demo.py # 真实起 uvicorn 子进程的 10 场景端到端演示
tests/           # pytest 自动化测试 (17 个)
requirements.txt # 直接依赖锁定版本; requirements.lock 为 pip freeze 全量锁定
```

---

## 3. 本地启动

```bash
cd /home/admin/Downloads/biaozhul/P050/b
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt          # 或 pip install -r requirements.lock

uvicorn app.main:app --host 127.0.0.1 --port 8000
# 数据默认在 ./data；可用 SNAPSHOT_HOME=/path/to/data 覆盖
# 交互文档: http://127.0.0.1:8000/docs
```

---

## 4. 验收命令

```bash
source .venv/bin/activate

# (1) 自动化测试: 17 个, 覆盖不可变性、篡改、缺产物、崩溃恢复、签名等
PYTHONPATH=. python -m pytest tests/ -v

# (2) 端到端完整演示: 真实 uvicorn 子进程 + HTTP, 含真实 os._exit 崩溃与重启
PYTHONPATH=. python -m demo.run_demo      # 端口可用 DEMO_PORT 覆盖, 默认 8765
```

演示会干净重建 `./data` 并依次跑通：

1. 注册输入并绑定不可变快照（PUT 被 405）；
2. 同快照同种子两次运行**逐字节一致**，换种子不同；
3. **发布新标定不改变旧快照运行**，引用新标定的新快照才不同；
4. 8 项校验全过 → 标记 `marked_reproducible`；
5. **测试输入被替换** → `input_verification_failed`，按哈希修复后恢复，拒绝错误“修复”；
6. **产物缺失/被改** → 先校验再发布，失败留错误证据、无索引，标记可复现返回 409；
7. **运行中改参数**：直接 UPDATE/DELETE 被 SQLite 触发器拒绝；运行期间发布新标定不影响在跑作业；
8. 重复执行累积为 attempt 1..5，不覆盖证据；
9. **进程真实崩溃 + 重启**：退出码 2，重启后 running→interrupted，重新尝试成功，恢复幂等；
10. 产物证据被删：索引与签名仍在但不能标记可复现，按内容恢复后重新通过。

---

## 5. HTTP 协议速览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/bags` (multipart `file`) | 上传 bag，返回 `bag-<sha256>` 与摘要 |
| POST | `/params` `/calibrations` `/algorithms` | 注册输入（内容相同幂等） |
| POST | `/snapshots` | 绑定四者为不可变快照 |
| GET | `/snapshots` `/snapshots/{id}` | 查询（无任何修改接口） |
| POST | `/snapshots/{id}/runs?seed=42` | 创建/获取 (快照,种子) 运行 |
| POST | `/runs/{id}/attempts` | 发起一次尝试，异步执行；body 可带 `delay_ms` 与故障注入 `fault` |
| GET | `/attempts/{id}` | 查状态（running/succeeded/failed/interrupted/marked_reproducible） |
| GET | `/attempts/{id}/artifacts/result\|error` | 取证据字节，响应头带 `Content-SHA256` |
| GET | `/attempts/{id}/verify` | 8 项可复现性校验明细 |
| POST | `/attempts/{id}/mark-reproducible` | 全过才标记，否则 409 附逐项原因 |
| PUT | `/evidence/{digest}` (multipart) | 仅允许用哈希一致的内容恢复证据 |
| POST | `/admin/recover` | 手动触发崩溃恢复（启动时已自动执行） |

`fault` 仅用于演示/测试失败路径：`lose_artifact`（产物缺失）、`corrupt_result`（运行期篡改产物）、
`crash`（延迟后 `os._exit(2)` 真实崩溃）。

### 手动最小流程

```bash
curl -s localhost:8000/bags -F file=@examples/bag.json
curl -s localhost:8000/params        -H 'content-type: application/json' --data @examples/params.json
CAL=$(jq -c .v1 examples/calibrations.json); curl -s localhost:8000/calibrations -H 'content-type: application/json' -d "$CAL"
ALGO=$(jq -c .v1 examples/algorithms.json); curl -s localhost:8000/algorithms -H 'content-type: application/json' -d "$ALGO"
# 用上面返回的 id 组装:
curl -s localhost:8000/snapshots -H 'content-type: application/json' \
  -d '{"bag_id":"bag-…","params_id":"params-…","calibration_id":"cal-…","algorithm_id":"algo-…"}'
curl -s -X POST 'localhost:8000/snapshots/snap-…/runs?seed=42'
curl -s -X POST localhost:8000/runs/run-…/attempts -H 'content-type: application/json' -d '{}'
curl -s localhost:8000/attempts/att-…/verify
curl -s -X POST localhost:8000/attempts/att-…/mark-reproducible
```

---

## 6. 失败如实报告

- 校验失败是正常业务结果：`failed` 状态 + 明确 `error_code` + 错误证据，HTTP 层用 409/422 表达；
- 演示中的崩溃是**真实的子进程退出（码 2）**与真实重启恢复，不是状态打桩；
- 哈希、HMAC 签名验签、确定性随机数、坐标变换与聚类均为真实计算，可用 `/verify` 的独立重算复核。
