# 离线软件更新元数据验证器

纯后端的 TUF 风格（The Update Framework）软件更新元数据验证服务：
**root / targets / snapshot / timestamp 四个角色各自用 Ed25519 独立签名**，
客户端按信任链逐级验证版本号、过期时间与哈希绑定；本地信任状态通过
**临时文件 + `fsync` + `os.replace` 原子提交**，任何验证失败都不会污染已信任状态。

技术栈：Python 3.12 · FastAPI · `cryptography`（Ed25519）· 无数据库（本地 JSON 状态 +
内容寻址 blob 文件）。

---

## 1. 信任模型

```
                ┌────────────────────────────────────────────┐
带外引导公钥 ──▶ │ root（keys/roles/threshold；信任锚）        │
                └────────────────────────────────────────────┘
                          签名授权            哈希+版本绑定
                 ┌────────────────────┐
                 │ timestamp（防冻结） │ ── 绑定 ──▶ snapshot 的 sha256/sha512/长度/版本
                 └────────────────────┘
                                             ┌──────────┐
                                   绑定 ────▶ │ snapshot │ ── 绑定 ──▶ targets.json 哈希/版本
                                             └──────────┘
                                                               ┌─────────┐
                                                     绑定 ──▶ │ targets │ ── 哈希 ──▶ 目标文件
                                                               └─────────┘
```

每条规则都有对应测试：

| 威胁 | 防线 |
|---|---|
| 回滚 / 冻结重放 | timestamp/snapshot/targets/root 版本号单调不减；root 必须逐版本 +1，禁止跳跃 |
| 混搭历史文件（mix-and-match） | timestamp→snapshot→targets 的哈希与长度逐字节绑定 |
| 根轮换攻击 | root v(n+1) 必须由 **v(n) 中声明的 root 角色密钥** 签名（授权链）；新密钥自签无效 |
| 伪造签名 / 密钥替换 | 所有信封经当前 root 授权的角色密钥按 threshold 多签校验；keyid = sha256(公钥) 且随签名一起验证 |
| 目标文件篡改 | targets 声明每个文件的 sha256+sha512+长度，下载内容逐项校验 |
| 无限期重放 | 四个角色均带 `expires`，过期即拒 |
| 更新中断 / 磁盘故障 | 验证全部通过后才原子替换 `state.json`；blob 内容寻址、先写后提交、孤儿自动清理 |

> 与标准 TUF 的一点差异：根轮换传播允许“同版本元数据用新角色密钥重签”，
> 但它仍必须通过**当前 root** 的签名校验和上层哈希绑定，因此篡改与旧密钥重放
> 依然被拒（见 `tests/test_acceptance_attacks.py` 中的
> `test_after_rotation_old_key_signed_same_version_metadata_rejected`）。

---

## 2. 目录结构

```
app/
  crypto_utils.py   Ed25519 签名/验签、keyid、多签阈值
  metadata.py       信封格式、规范化 JSON（签名/哈希的统一字节口径）、过期检查
  verifier.py       信任链验证 + 原子提交（核心）
  storage.py        TrustStore：flock 互斥、原子 state.json、内容寻址 blob
  builder.py        发布端构建器（测试/演示用，也可作为发布工具参考）
  errors.py         机器可读错误码 → HTTP 状态码
  config.py         环境变量配置
  main.py           FastAPI 应用与 HTTP 接口
scripts/
  make_demo_repo.py 生成 demo/ 演示仓库（合法版本 + 4 类攻击包 + curl 脚本）
tests/              37 个自动化测试（pytest）
requirements.txt    直接依赖（精确版本）
requirements-lock.txt  全部传递依赖锁定版本
```

---

## 3. 依赖与安装

需要 Python 3.12（3.10+ 语法即可，实际在 3.12.3 上测试）。

```bash
python3 -m venv .venv
source .venv/bin/activate

# 方式 A：按锁定文件复现（推荐）
pip install -r requirements-lock.txt

# 方式 B：只装直接依赖
pip install -r requirements.txt
```

直接依赖：`fastapi==0.115.5`、`uvicorn[standard]==0.32.1`、
`cryptography==44.0.0`、`python-multipart==0.0.20`；
测试额外需要 `pytest==8.3.4`、`httpx==0.28.1`。

---

## 4. 启动命令

```bash
# 先生成演示仓库（其中 bootstrap_root_public.txt 是带外信任锚）
python scripts/make_demo_repo.py demo

# 启动服务
BOOTSTRAP_ROOT_PUBLIC=$(cat demo/bootstrap_root_public.txt) \
DATA_DIR=./data \
ADMIN_TOKEN=change-me \
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

环境变量：

| 变量 | 说明 | 默认 |
|---|---|---|
| `BOOTSTRAP_ROOT_PUBLIC` | 带外引导 root 公钥（hex）。首次安装 root v1 必须由它签名 | 无（未设置时拒绝引导） |
| `DATA_DIR` | 本地信任状态与 blob 目录 | `./data` |
| `ADMIN_TOKEN` | `/admin/reset` 管理令牌；不设置则管理接口禁用 | 无 |

---

## 5. HTTP 接口

### `GET /health`
返回各角色已信任版本与已安装目标清单。

### `POST /updates` （核心接口，`multipart/form-data`）

| 表单字段 | 说明 |
|---|---|
| `root` | root 元数据 JSON 文件；**首次安装必填**，之后可选（轮换时必带） |
| `timestamp` / `snapshot` / `targets` | 三个角色元数据 JSON 文件，必填 |
| `files` | 目标文件，可重复出现；用 `filename=` 指定目标名（curl）或 `?name=` 查询参数 |

验证通过返回 200 与版本摘要；失败返回 4xx/409/422，错误体形如
`{"error": {"code": "...", "message": "...", "detail": {...}}}`，且本地状态不变。

### `GET /targets/{name}`
下载已通过验证的目标文件；响应头回带 `X-Content-Sha256` / `X-Content-Sha512`。

### `GET /metadata/{role}`
读取本地已信任的角色元数据原文（`root|targets|snapshot|timestamp`）。

### `POST /admin/reset`
`Authorization: Bearer <ADMIN_TOKEN>`，清空全部信任状态。

### curl 请求样例

```bash
# 引导安装 v1
curl -sS -X POST http://127.0.0.1:8000/updates \
  -F "root=@demo/v1/root.json;type=application/json" \
  -F "timestamp=@demo/v1/timestamp.json;type=application/json" \
  -F "snapshot=@demo/v1/snapshot.json;type=application/json" \
  -F "targets=@demo/v1/targets.json;type=application/json" \
  -F "files=@demo/v1/files/app.bin;filename=app.bin"

# 更新到 v2（已信任 root 后无需再带 root）
curl -sS -X POST http://127.0.0.1:8000/updates \
  -F "timestamp=@demo/v2/timestamp.json;type=application/json" \
  -F "snapshot=@demo/v2/snapshot.json;type=application/json" \
  -F "targets=@demo/v2/targets.json;type=application/json" \
  -F "files=@demo/v2/files/app.bin;filename=app.bin" \
  -F "files=@demo/v2/files/notes.txt;filename=notes.txt"

# 下载
curl -sS http://127.0.0.1:8000/targets/app.bin
```

更多完整样例（含 4 类攻击与根轮换恢复）由生成器输出到
`demo/curl-examples.sh`，可直接 `BASE=http://127.0.0.1:8000 bash demo/curl-examples.sh`。

---

## 6. 元数据格式

四个角色统一信封，签名针对 `signed` 的**规范化 JSON**
（键排序、无空白、UTF-8、末尾换行）：

```json
{
  "signatures": [{"keyid": "<sha256(pubkey) hex>", "sig": "<ed25519 hex>"}],
  "signed": {
    "_type": "root | targets | snapshot | timestamp",
    "version": 1,
    "expires": "2026-10-24T00:00:00Z",
    "...": "角色专属字段"
  }
}
```

- root：`keys`（keyid → 公钥对象）、`roles`（每角色 `keyids` + `threshold`）；
- timestamp：`meta.snapshot = {version, length, hashes}`；
- snapshot：`meta["targets.json"] = {version, length, hashes}`；
- targets：`targets = {名称: {length, hashes: {sha256, sha512}}}`。

---

## 7. 自动化测试

```bash
pytest -q
```

测试文件：

- `tests/test_verifier.py` — 引导、幂等重提、合法版本推进、签名阈值、过期、
  文件哈希/长度、多余/缺失文件；
- `tests/test_acceptance_attacks.py` — **验收场景**：混搭历史文件、冻结旧时间戳、
  合法/非法根轮换、root/snapshot/整包回滚、轮换后旧密钥重放，并验证每次拒绝后
  合法下一版本可恢复；
- `tests/test_storage_atomic.py` — 失败不留半成品、原子替换、无临时文件残留、
  内容寻址去重、reset；
- `tests/test_http_api.py` — 真实 ASGI 端到端：引导、更新、下载、攻击返回错误码、
  根轮换、管理接口。

### 实际运行结果（本机，Python 3.12.3）

```
37 passed, 1 warning in 0.9s
```
（唯一 warning 来自 starlette TestClient 对 anyio 弃用别名，与本项目代码无关。）

### 真实服务端到端演练结果

对真实 `uvicorn` 服务依次执行 `demo/curl-examples.sh` 的实际结果：

| 步骤 | HTTP | 结果 |
|---|---|---|
| 安装 v1（引导） | 200 | root/timestamp/snapshot/targets 均为 v1，`app.bin` 入库 |
| 更新到 v2 | 200 | 三角色推进到 v2，新增 `notes.txt` |
| 攻击 1：混搭历史文件 | 422 | `HASH_MISMATCH`（snapshot 与 timestamp 绑定不符） |
| 攻击 2：冻结重放 v1 | 409 | `TIMESTAMP_ROLLBACK`（已信任 v2，收到 v1） |
| 攻击 4：篡改目标文件 | 422 | `TARGET_HASH_MISMATCH`（sha256 不符） |
| 攻击 3：新密钥自签 root | 401 | `THRESHOLD_NOT_MET`（0/1 个合法签名） |
| 合法 v3：授权根轮换 | 200 | root 推进到 v2，`root_rotated=true`，其余角色保持 v2 |
| `/health` 最终状态 | 200 | root v2，timestamp/snapshot/targets v2，两个目标文件已安装 |

---

## 8. 未完成项 / 已知限制（如实记录）

- **无委托角色（targets delegations）、无离线/在线职责拆分的快照保持期配置**：
  是 TUF 的精简实现，只有四个顶层角色。
- **无时间戳“静默同版本重签”处理策略**：若发布方仅延长 `expires` 而不推进版本号，
  当前规则要求该同版本内容同时通过当前 root 签名校验与上层哈希绑定；常规发布应推进版本号。
- **root 不带 `consistent_snapshot` 的时间机器处理**：未实现 TUF 1.0 §6 的
  持久化旧版本快照目录；本服务只保留当前已信任版本，靠版本单调+哈希绑定防回滚。
- **并发上传**：同进程 asyncio 锁 + `flock` 跨进程互斥已具备，但未做高并发压测。
- **管理接口仅 Bearer 令牌**，没有审计日志；生产部署应放到本地回环或加网络层鉴权。
- 未做 Windows 平台验证（`flock` 为 POSIX；原子 replace 在 Windows 上语义略有差异）。
