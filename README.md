# 机器人实验快照服务（Robot Experiment Snapshot Service）

纯后端服务，用 **Python + FastAPI + SQLite** 实现机器人**离线实验的可复现快照**：
把 **bag 摘要、运行参数、坐标标定、算法版本**绑定为一个**不可变**、经
**Ed25519 数字签名**的实验快照；作业只读取快照；产物在发布索引前先逐项校验；
重复执行按内容留存为不同尝试，不覆盖证据；缺输入或种子则不能标记为可复现。

所有计算与密码操作都是**真实执行**（确定性数值管线 + SHA-256 + Ed25519 签名/验签），
故障场景如实报告，无任何模拟桩。

---

## 1. 它保证了什么

| 需求 | 实现方式 |
| --- | --- |
| 不可变快照 | 快照 `id = sha256(canonical(manifest))`，manifest 含 bag 摘要+参数+标定摘要+算法名/版本；内容相同则 id 相同（去重），内容不同则 id 不同。快照写入后不可修改，且带 Ed25519 分离签名。 |
| 作业只读固定快照 | 作业在启动时把快照所需内容（参数、标定矩阵、bag 摘要）全部从快照行读取；**之后发布新标定不会影响任何已有作业的结果**。 |
| 新标定不改变运行结果 | 标定以其内容摘要作为 id；发布标定 v2 只会产生一个新 id。旧快照仍绑定旧标定摘要；只有显式用新标定创建**新快照**才会改变结果。 |
| 可复现判定 | 产物记录 **输入摘要 `input_digest` 与运行种子 `seed`**。运行开始与结束分别计算输入摘要与参数摘要，二者一致、产物存在且摘要匹配、并用当前数据**真实重算**管线与产物结果逐项比对，全部通过才 `reproducible=true`。**缺一项即不可复现。** |
| 先校验再发布索引 | `POST /indexes` 先对每个作业做全部校验（含产物存在性、摘要、重算、签名），任一失败返回 `409` 且**不写任何索引文件**；全部通过后才生成、签名并原子落盘追加式索引（`index-0001.json`、`index-0002.json`…）。 |
| 重复执行不覆盖证据 | 每次运行都是新 `job_id`、独立证据目录 `evidence/jobs/<job_id>/result.json`；同内容重跑产物字节相同但证据各自留存。篡改时原始文件被改名保留（`*.original-evidence` / `*.removed-evidence`），永不覆盖。 |
| 测试输入被替换 | bag 以 sha256 内容寻址存放；校验时重新计算磁盘 bag 摘要并**真实重跑**管线，摘要不符或结果不符即标记 `input_bag_digest_mismatch` / `nondeterministic_rerun_result`。 |
| 产物缺失/被换 | `output_artifact_missing` / `output_digest_mismatch`，并阻止进入索引。 |
| 运行中改参数 | 记录起止参数摘要，不一致 → `parameters_changed_during_run` + `input_digest_mismatch_start_vs_end`，不可复现、禁止发布。 |
| 运行中重启 | 服务启动时 `recover_after_restart()` 把所有遗留 `running` 作业置为 `failed`、不可复现（`recovered_after_restart`），记录时间戳事件；签名密钥持久化（`signer_key.pem`，权限 0600），重启后旧签名与已发布索引仍可验签。 |

---

## 2. 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由与启动（启动时重启恢复）
│   ├── storage.py     # SQLite 存储 + 磁盘证据库 + 作业执行/校验/索引/重启恢复
│   ├── compute.py     # 真实确定性数值管线（解析 bag、标定变换、加权统计、种子化 score）
│   ├── crypto.py      # SHA-256、规范 JSON、Ed25519 签名/验签（cryptography 库，真实）
│   └── models.py      # Pydantic 请求/响应模型
├── examples/          # 小型合成输入：bag、bag 摘要、标定、参数
├── scripts/
│   ├── demo.py        # 端到端验收演示（真实 uvicorn 子进程 + 真实 SIGKILL 重启）
│   └── demo.sh        # 建虚拟环境装锁并运行 demo
├── tests/             # pytest 自动化测试（密码、计算、API、故障、重启）
├── conftest.py
├── requirements.txt   # 直接依赖（固定版本）
└── requirements.lock  # pip freeze 全量锁定依赖（28 个包）
```

证据目录（默认 `./data`，可用环境变量 `SNAPSHOT_ROOT` 改）：

```
<root>/
├── signer_key.pem                  # Ed25519 私钥（0600，跨重启复用）
├── state.db                        # SQLite（WAL）
└── evidence/
    ├── bags/<sha256>.bin           # 内容寻址输入，永不改名
    ├── jobs/<job_id>/result.json   # 每次尝试独立目录，不覆盖
    └── indexes/index-000N.json     # 追加式、已签名的发布索引
```

---

## 3. 本地启动

需要 Python 3.11+（开发环境为 3.12）。

```bash
cd /home/admin/Downloads/biaozhul/P050/a
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock          # 安装锁定依赖

# 启动（默认 127.0.0.1:8000；数据目录默认 ./data）
uvicorn app.main:app --reload
# 或：python -m app.main   （可用 HOST / PORT / SNAPSHOT_ROOT 环境变量）
```

打开交互式文档： <http://127.0.0.1:8000/docs>
查看验签公钥： `GET http://127.0.0.1:8000/public-key`

---

## 4. 验收命令

### 4.1 自动化测试（34 个，全部通过）

```bash
source .venv/bin/activate
python -m pytest -q
```

覆盖：SHA-256 已知向量、规范 JSON 顺序无关、Ed25519 签名/验签/错误密钥拒绝/跨重启密钥持久化；
管线确定性与种子/参数/标定/bag 敏感性；快照内容 id 与签名；新标定不影响旧作业；
缺输入/种子不可复现；改参数、换产物、缺产物、换 bag；校验失败不发布索引；真实重启恢复。

### 4.2 端到端验收演示（27 项检查，含真实进程崩溃重启）

```bash
source .venv/bin/activate
python scripts/demo.py        # 或 ./scripts/demo.sh
```

它会启动一个**真实 uvicorn 子进程**，完整走一遍：上传 bag → 发布标定 →
冻结签名快照 → 可复现作业（记录输入与种子）→ 发布新标定但结果不变 →
重复执行产生独立尝试且字节一致 → 先校验后发布并验签索引 →
**替换测试输入 / 删除产物 / 运行中改参数 / 运行中 SIGKILL 真实重启**，
最后打印 `27/27 checks passed`。证据保留在 `./data_demo/`。

### 4.3 手工 curl 快速验收

```bash
# 1) 标定（返回其内容摘要作为 id）
curl -s -X POST localhost:8000/calibrations \
  -H 'Content-Type: application/json' \
  --data @examples/calibration.json

# 2) 上传合成 bag（multipart：file + summary JSON）
curl -s -X POST localhost:8000/bags \
  -F "file=@examples/bag_alpha.csv;type=text/csv" \
  -F "summary=<examples/bag_summary.json"

# 3) 用上面返回的 calibration id 与 bag digest 创建快照
curl -s -X POST localhost:8000/snapshots -H 'Content-Type: application/json' -d '{
  "bag_digest": "<bag-digest>",
  "params": {"gain":1.5,"offset":0.01,"filter_width":2.0},
  "calibration_id": "<calibration-id>",
  "algorithm_name":"pointcloud-transform","algorithm_version":"3.2.1"}'

# 4) 同步跑一次（记录 seed 与 input_digest）
curl -s -X POST localhost:8000/jobs/run-sync -H 'Content-Type: application/json' \
  -d '{"snapshot_id":"<snapshot-id>","seed":42}'

# 5) 先校验再发布索引（用返回的 job id）
curl -s -X POST localhost:8000/indexes -H 'Content-Type: application/json' \
  -d '{"job_ids":["<job-id>"]}'

# 6) 独立验签已发布索引
curl -s localhost:8000/indexes/<index-id>/verify
```

---

## 5. HTTP API 摘要

| 方法 & 路径 | 说明 |
| --- | --- |
| `GET  /health` | 健康检查；返回本次启动恢复的作业 id 列表 |
| `GET  /public-key` | Ed25519 验签公钥（PEM） |
| `POST /bags` | 上传 bag 字节（multipart `file` + `summary` JSON），返回 sha256 |
| `GET  /bags` | 列出已存 bag |
| `POST /calibrations` | 发布坐标标定（4×4 变换+标记点+重投影误差），id=内容摘要 |
| `GET  /calibrations` , `/calibrations/{id}` | 查询标定 |
| `POST /snapshots` | 冻结不可变签名快照 |
| `GET  /snapshots` , `/snapshots/{id}` , `/snapshots/{id}/verify` | 查询/验签 |
| `POST /jobs` | 后台执行作业（202，支持 `hold_sec`、`simulate_param_change` 故障注入） |
| `POST /jobs/run-sync` | 同步执行并返回最终状态 |
| `GET  /jobs?snapshot_id=…` , `/jobs/{id}` | 查询作业（含 `reproducible` 与失败原因） |
| `GET  /jobs/{id}/artifacts/result.json` | 取产物（缺失 404，被换成非法 JSON 返回 409） |
| `POST /jobs/{id}/revalidate` | 重新做全部证据校验并重算，更新可复现判定 |
| `POST /jobs/{id}/simulate-tamper?mode=swap|missing` | **仅演示用**故障注入（保留原始证据） |
| `POST /indexes` | **先校验后发布**；body `{"job_ids":[...]}`，失败返回 409 且不发布 |
| `GET  /indexes` , `/indexes/latest` , `/indexes/{id}` | 查询索引 |
| `GET  /indexes/{id}/verify` | 独立验签索引并逐个核对产物摘要 |

作业对象关键字段：`status`（running/completed/failed）、`reproducible`、
`input_digest` / `input_digest_end`、`params_digest_start` / `params_digest_end`、
`output_digest`、`seed`、`reproducibility_failures`、`tamper_events`。

---

## 6. 合成 bag 格式与确定性说明

`examples/bag_alpha.csv` 为小型合成数据，每行 `X,Y,Z[,权重]`，支持 `#` 注释与空行；
管线解析点云、施加快照中的 4×4 标定齐次变换与参数（`gain/offset`），
计算加权质心与加权方差，并把「几何摘要 + 种子」做 SHA-256 密钥混合得到确定性 `score`。

- 相同（bag、参数、标定、种子）→ 产物**逐字节相同**（不使用系统熵/`random`）。
- 几何量与种子无关；种子只影响 `score`，因此不同种子几何一致而分数不同。
- 产物为规范 JSON（键排序、紧凑分隔符），原子写盘（临时文件 + `os.replace` + `fsync`）。

---

## 7. 故障矩阵（均有测试与演示覆盖）

| 故障 | 检测点 | 结果 |
| --- | --- | --- |
| 测试输入 bag 被替换 | 重新算 bag sha256 + 真实重算结果 | 不可复现，禁止发布 |
| 产物文件缺失 | 发布前/重校验存在性检查 | `output_artifact_missing`，409 不发布 |
| 产物内容被换 | 重算文件 sha256 与记录比对 | `output_digest_mismatch`，原始证据保留 |
| 运行中改参数 | 起止参数摘要/输入摘要比对 | 不可复现，409 不发布 |
| 运行中进程崩溃重启 | 启动恢复扫描 `running` | 置 failed、不可复现，旧索引仍可验签 |

失败一律如实返回具体原因码（见 `reproducibility_failures` / 409 `detail`），
不做静默忽略。
