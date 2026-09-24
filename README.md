# 制品签名与信任轮换服务（Artifact Signing & Trust Root Rotation）

纯后端 HTTP 服务，基于成熟库 **Ed25519（`cryptography`）** 实现本地制品签名/验证与信任根轮换。

- 制品签名**绑定内容摘要（SHA-256）、制品类型、版本**，以及签名者一次性 nonce；
- 信任根轮换必须由**旧根的根角色密钥达到阈值批准**；
- **拒绝版本回退**（新版本只能 `当前版本 + 1`）；
- **角色分离**（TUF 风格）：根角色只批准轮换，制品角色只签制品，互不兼任；
- 防重复签名：同一签名者对同一制品的多签只计一票；制品不可重复登记；
- 轮换生效后旧根/旧签名者即刻失效。

服务端**从不持有私钥**：签名只在本地用 `scripts/` 完成，服务端只接收公钥与签名。

---

## 1. 目录结构

```
app/
  canonical.py   确定性报文编码（域分隔 + 长度前缀，防字段拼接歧义）
  keys.py        Ed25519 密钥生成/序列化/签名验证（cryptography）
  models.py      Pydantic 请求/响应模型（额外字段一律拒绝）
  errors.py      统一错误码
  store.py       信任库：引导、验证、登记、阈值轮换、防回退、落盘
  main.py        FastAPI 应用与 HTTP 路由
scripts/
  generate_keys.py  生成全部测试密钥（仅测试，非生产凭据）
  make_examples.py  生成 examples/ 下全部 HTTP 请求样例
  sighelp.py        本地签名辅助（制品签名 / 根批准签名）
tests/           pytest 自动化测试（46 个用例）
examples/        12 个 curl 请求样例（由脚本生成）
requirements.txt        运行依赖（锁定）
requirements-lock.txt   全量锁定（含 pytest/httpx）
```

## 2. 环境与依赖

- Python 3.10+（开发实测 3.12）
- 运行依赖（见 `requirements.txt`，已锁定）：`fastapi 0.141.1`、`uvicorn 0.53.0`、
  `cryptography 50.0.1`、`pydantic 2.x`
- 测试依赖：`pytest`、`httpx`（见 `requirements-lock.txt`）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-lock.txt   # 复现开发环境（含测试工具）
# 仅运行服务：pip install -r requirements.txt
```

## 3. 启动命令

```bash
source .venv/bin/activate

# 纯内存模式（重启清空，适合演示）
uvicorn app.main:app --host 127.0.0.1 --port 8000

# 落盘模式（状态 JSON 持久化，文件中只含公钥，绝无私钥）
TRUST_STORE_FILE=./data/trust-state.json uvicorn app.main:app --host 127.0.0.1 --port 8000
```

启动后：
- 交互式文档：http://127.0.0.1:8000/docs （Swagger UI）
- OpenAPI：http://127.0.0.1:8000/openapi.json
- 健康检查：`GET /health`

## 4. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查与当前根版本 |
| GET | `/roots/current` | 查看当前信任根（无引导时返回 `null`） |
| POST | `/roots/bootstrap` | 引导初始信任根（version 必须为 1，只能一次） |
| POST | `/roots/rotate` | 阈值批准下轮换信任根（拒绝回退/跳号） |
| POST | `/verify` | 验证制品信封（可选附带正文重算摘要） |
| POST | `/artifacts/register` | 验证达阈值后登记制品（防重复登记/重复签名） |

### 报文设计

- **制品签名报文**（Ed25519 签名对象）：
  `ARTIFACT-SIGNATURE/v1` 域 + `SHA256(内容)` + `artifact_type` + `version` + 签名者一次性 `nonce`，
  每个字段带 8 字节长度前缀。
  因此改动正文（摘要变）、换类型、换版本、或把签名搬到别的内容上，都会令签名校验失败。
- **根描述符报文**：`TRUST-ROOT-DESCRIPTOR/v1` + 版本 + 根阈值 + 根公钥集合（排序）
  + 制品阈值 + 制品公钥集合（排序）。
- **轮换批准报文**：`TRUST-ROOT-ROTATION-APPROVAL/v1` + 新根描述符 + 一次性 nonce。
  必须由**旧根**的根角色私钥签名，独立有效签名者数量达到旧根 `root_threshold` 才生效。

## 5. 快速上手：端到端请求样例

```bash
# 0) 生成全部测试密钥与请求样例（test-keys/ 已 gitignore）
python scripts/generate_keys.py test-keys
python scripts/make_examples.py

# 1) 引导初始根：3 个根角色密钥（阈值 2）、3 个制品签名密钥（阈值 2）
curl -s -X POST http://127.0.0.1:8000/roots/bootstrap \
  -H 'Content-Type: application/json' -d @examples/01_bootstrap.json

# 2) 验证合法制品（2/2 签名，并附带正文重算摘要）
curl -s -X POST http://127.0.0.1:8000/verify \
  -H 'Content-Type: application/json' -d @examples/02_verify_valid.json

# 3) 登记制品
curl -s -X POST http://127.0.0.1:8000/artifacts/register \
  -H 'Content-Type: application/json' -d @examples/03_register.json
```

### 验收四类攻击/异常（均应拒绝）

```bash
# 正文篡改：content 与 digest 不符 -> digest_mismatch_content_tampered
curl -s -X POST .../verify -d @examples/04_verify_content_tampered.json

# 跨类型复用：为 report 签的名拿去当 container-image -> signature_mismatch
curl -s -X POST .../verify -d @examples/05_verify_cross_type.json

# 版本不一致复用：为 1.0.0 签的名声称 0.9.0 -> signature_mismatch
curl -s -X POST .../verify -d @examples/06_verify_version_mismatch.json

# 重复签名：同一签名块放两遍 -> duplicate_signer_block，只计 1 票，阈值不足
curl -s -X POST .../verify -d @examples/07_verify_duplicate_signature.json

# 未授权/失效签名者：根密钥签制品（或轮换后旧签名者）-> signer_not_authorized
curl -s -X POST .../verify -d @examples/08_verify_unauthorized_signer.json
```

### 信任根轮换

```bash
# 合法轮换：v1 -> v2，附 2 个旧根密钥的批准签名
curl -s -X POST http://127.0.0.1:8000/roots/rotate \
  -H 'Content-Type: application/json' -d @examples/09_rotate_v2.json

# 版本回退（提交 version=1）-> 403 version_rollback_rejected
curl -s -i -X POST .../roots/rotate -d @examples/10_rotate_rollback_rejected.json

# 批准不足（少于旧根阈值）-> 403 rotation_unauthorized
curl -s -i -X POST .../roots/rotate -d @examples/11_rotate_insufficient_approvals.json

# 轮换后旧签名者立即失效
curl -s -X POST .../verify -d @examples/12_verify_after_rotation_old_signer.json
```

> 注：`examples/11_*` 以「当前处于 v1」为前提；若服务已先轮换到 v2，
> 版本检查会先于阈值检查返回回退错误。干净内存状态下依次执行即可看到
> `rotation_unauthorized`；该情形在自动化测试中亦有覆盖。

## 6. 自动化测试

```bash
source .venv/bin/activate
python -m pytest -q
```

测试在进程内用 `fastapi.testclient` 完成，密钥每次测试现场生成，不落盘到
`test-keys/`。覆盖：签名/验签往返、正文篡改、digest 篡改、跨类型、跨版本、
签名搬到其他内容、重复签名块、重复登记、重叠签名者重提、未授权角色、
外部未知密钥、非法 base64/公钥、引导规则、阈值/角色分离校验、
轮换阈值不足/成功、回退/同版本/跳号、制品密钥无权批准、新根密钥无权批准、
重复批准只计一票、批准绑定精确描述符、轮换后旧签名者失效、链式 v1→v2→v3、
旧根密钥在 v3 下失效、状态持久化与重启恢复（落盘文件不含私钥）。

## 7. 安全说明与边界

- 所有 `test-keys/` 密钥均为本机 `Ed25519PrivateKey.generate()` 即时生成的
  **测试密钥**，无任何生产凭据；该目录已在 `.gitignore` 中忽略。
- 这是教学/演示级服务：状态为内存或单文件 JSON，未做并发分布式共识、
  多副本时间戳、快照/过期（TUF 的 timestamp/snapshot 角色）等生产级机制；
  不可直接作为生产更新框架使用。
