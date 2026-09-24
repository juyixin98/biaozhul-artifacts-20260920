# 离线软件更新元数据验证器

纯后端实现的 **TUF（The Update Framework）风格**离线软件更新验证服务：
`root / targets / snapshot / timestamp` 四级元数据各自独立签名，
更新时逐级验证签名、版本、过期时间与哈希绑定；本地信任状态以
**单文件原子替换**方式提交，任何验证失败都不会污染已提交状态。

技术栈：Python 3.10+ · FastAPI · `cryptography`（Ed25519）· pytest。

---

## 1. 信任模型

| 角色 | 作用 | 关键约束 |
|---|---|---|
| `root` | 信任锚：公钥库 + 各角色 keyid/阈值 | 引导时带外分发、自签名；轮换版本只能 **+1**，且新 root 必须被**旧 root 与新 root 双方**的 root 密钥签名 |
| `timestamp` | 最外层，最频繁轮换 | 版本不得回退/重放；不得过期；`meta.snapshot.json` 的 `length + sha256` 绑定 snapshot 字节 |
| `snapshot` | 固定 targets 等角色 | 版本不得回退；不得过期；`meta.targets.json` 的 `length + sha256` 绑定 targets 字节，并绑定其版本 |
| `targets` | 目标文件清单 | 版本不得回退；不得过期；每个目标的 `length + sha256` 绑定实际文件字节 |

每次更新的验证顺序：

1. **root（可选轮换）**：版本必须恰好 +1；新旧双方授权签名；未过期。
2. 解析 timestamp / snapshot / targets（结构、体积上限）。
3. 用（轮换后的）root 公钥校验三段签名，阈值不足即拒。
4. **过期检查**（先于版本检查——冻结攻击的核心防线）。
5. **版本回退/重放检查**：低于当前版本、与当前版本相同都拒绝；
   引导后的首个更新三段版本必须从 v1 开始。
6. **跨角色绑定**：timestamp→snapshot、snapshot→targets 的哈希、长度、版本一致。
7. **目标文件绑定**：随附文件集合必须与 targets 声明集合完全一致，逐文件校验长度+sha256。
8. 全部通过后才进入提交阶段。

> 安全取舍说明：第 4 步刻意排在版本检查之前。这样“攻击者重放一份
> 签名合法但已过期的旧 timestamp”会被明确标记为 `expired`，
> 而不是被回滚规则抢先拦住——两条防线独立可观测。

### 原子提交

- 数据目录中 `trust-state.json` 是**唯一**的信任状态；
  写入走“同目录临时文件 → `fsync` → `os.replace()` → fsync 目录”，
  在 POSIX 上是原子的。
- 目标文件按 sha256 内容寻址存入 `store/`，同样用临时文件 + rename 落盘。
- 提交顺序：先原子落目标文件，再切换状态文件。“root 轮换中断”
  （在状态切换前崩溃）后，旧状态文件字节完全不变，用合法包可重新提交恢复。
- 所有操作持有 `fcntl.flock` 排他锁，防止并发更新交错。

---

## 2. 目录结构

```
app/
  __init__.py
  errors.py       # 拒绝原因的异常类型（对应稳定错误码）
  canonical.py    # TUF 规范化 JSON（签名负载的确定性编码）
  crypto.py       # Ed25519 签发/验证、keyid 计算
  metadata.py     # 四种元数据的解析、结构校验与构建辅助
  repo_tool.py    # 签发端工具（服务端本身不持有私钥）
  updater.py      # 验证核心 + TrustStore（原子状态/文件锁/内容寻址仓库）
  api.py          # FastAPI 接口层
scripts/
  make_samples.py # 生成演示用样例仓库（合法链 + 三个攻击包）
tests/            # 46 个 pytest 用例
requirements.txt       # 运行时直接依赖
requirements.lock.txt  # 本次实测的精确版本（锁定）
pyproject.toml
```

---

## 3. 安装与启动

需要 Python 3.10+（开发实测 3.12.3）。

```bash
# 1) 建议使用虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 2) 安装依赖（二选一）
pip install -r requirements.txt              # 直接依赖
pip install -r requirements.lock.txt         # 或安装本次锁定的精确版本
pip install pytest httpx                     # 仅跑测试时需要

# 3) 启动服务
uvicorn app.api:app --host 127.0.0.1 --port 8000
# 可选：自定义数据目录（默认 ./data）
UPDATER_DATA_DIR=/var/lib/updater uvicorn app.api:app
```

启动后：

- 交互文档：<http://127.0.0.1:8000/docs>
- 健康检查：`GET /api/health`

---

## 4. HTTP 接口与请求样例

所有元数据和文件通过 `multipart/form-data` 上传（便于 curl 演示）。
验证失败统一返回：

```json
{"accepted": false, "error": "<错误码>", "message": "<中文说明>"}
```

错误码：`malformed`（400）、`signature`（400）、`expired`（400）、
`rollback`（400）、`binding`（400）、`hash`（400）、
`no_state`（409）、`state_exists`（409）、`target_not_found`（404）。

### 4.1 生成样例仓库

```bash
python scripts/make_samples.py samples
```

得到：`1-root/`（引导）、`2-v1/ 3-v2/ 4-v3-rotated/`（合法升级，
最后一个携带新 root 完成 A→B 轮换）、`attack-rollback/`、
`attack-frozen/`、`attack-payload-swap/`（三个攻击包）。

### 4.2 引导（带外信任 root）

```bash
curl -X POST http://127.0.0.1:8000/api/bootstrap \
  -F "root=@samples/1-root/1.root.json;type=application/json"
# {"status":"bootstrapped","root_version":1,"accepted":true}
```

### 4.3 提交更新

```bash
curl -X POST http://127.0.0.1:8000/api/update \
  -F "timestamp=@samples/2-v1/timestamp.json;type=application/json" \
  -F "snapshot=@samples/2-v1/snapshot.json;type=application/json" \
  -F "targets=@samples/2-v1/targets.json;type=application/json" \
  -F "files=@samples/2-v1/app.txt;filename=app.txt;type=application/octet-stream"
# root 轮换时额外加： -F "root=@samples/4-v3-rotated/root.json;type=application/json"
```

字段名固定：`timestamp` / `snapshot` / `targets` / 可选 `root`；
目标文件统一用可重复的 `files` 字段（`filename` 即目标名，不允许路径分隔符）。

成功响应：

```json
{"status":"updated","accepted":true,
 "versions":{"root":1,"targets":1,"snapshot":1,"timestamp":1},
 "targets":["app.txt"]}
```

### 4.4 攻击包全部被拒（在 v3 轮换后依次提交）

```bash
# 回滚：当前受信 B 密钥合法签名的 v2 重放
curl -X POST .../api/update -F timestamp=@samples/attack-rollback/... # 400 rollback
# 冻结：合法签名但 timestamp 昨天过期
curl -X POST .../api/update -F timestamp=@samples/attack-frozen/...   # 400 expired
# 掉包：B 合法签出全新 v4 元数据，但随附文件内容与 sha256 不符
curl -X POST .../api/update -F files=@samples/attack-payload-swap/... # 400 hash
```

### 4.5 查询与下载

```bash
curl http://127.0.0.1:8000/api/state                 # 当前版本与目标清单
curl http://127.0.0.1:8000/api/metadata/timestamp    # 已接受的某角色原始元数据
curl http://127.0.0.1:8000/api/targets/app.txt -O    # 下载（出库前再次校验哈希）
curl -X POST http://127.0.0.1:8000/api/reset         # 清空状态（演示/测试用）
```

### 4.6 Python 客户端样例

```python
import httpx

with open("samples/1-root/1.root.json", "rb") as f:
    httpx.post("http://127.0.0.1:8000/api/bootstrap",
               files={"root": ("1.root.json", f, "application/json")})

with open("samples/2-v1/timestamp.json", "rb") as ts, \
     open("samples/2-v1/snapshot.json", "rb") as sn, \
     open("samples/2-v1/targets.json", "rb") as tg, \
     open("samples/2-v1/app.txt", "rb") as app:
    r = httpx.post("http://127.0.0.1:8000/api/update", files=[
        ("timestamp", ("timestamp.json", ts, "application/json")),
        ("snapshot", ("snapshot.json", sn, "application/json")),
        ("targets", ("targets.json", tg, "application/json")),
        ("files", ("app.txt", app, "application/octet-stream")),
    ])
    r.raise_for_status()
    print(r.json())
```

---

## 5. 元数据格式（JSON）

签名负载使用规范化 JSON（键排序、无空白、小写 hex、无浮点），
外层信封：`{"signatures":[{"keyid","sig"}], "signed": {...}}`，
`sig` 为 64 字节 Ed25519 签名的 hex，签名内容是 `signed` 段的规范化字节。

```jsonc
// root
{"_type":"root","version":1,"expires":"2036-01-01T00:00:00Z",
 "keys":{"<sha256 keyid>":{"keytype":"ed25519","scheme":"ed25519",
                           "keyval":{"public":"<32 字节公钥 hex>"}}},
 "roles":{"root":{"keyids":[...],"threshold":1}, "...": {...}}}
// targets
{"_type":"targets","version":1,"expires":"2026-12-01T00:00:00Z",
 "targets":{"app.txt":{"length":36,"hashes":{"sha256":"..."}}}}
// snapshot / timestamp
{"_type":"snapshot","version":1,"expires":"...",
 "meta":{"targets.json":{"version":1,"length":N,"hashes":{"sha256":"..."}}}}
```

体积上限：单份元数据 1 MiB，单个目标 100 MiB（在 `app/updater.py` 可调）。

---

## 6. 自动化测试

```bash
pip install pytest httpx
python -m pytest
```

覆盖的安全属性（详见 `tests/`）：

- 规范化 JSON 确定性、Ed25519 签发/验证、keyid 一致性；
- 引导：自签名 root、无签名/过期/重复引导拒绝；
- 合法升级链 v1→v2→v3 与下载内容核对；
- **回滚**：版本倒退、同版本重放、首包非 v1；
- **冻结**：合法签名的过期 timestamp 重放被拒，新的合法下一版本可恢复；
- **混搭历史文件**：timestamp↔snapshot、snapshot↔targets 的哈希/版本不一致；
- 目标文件掉包、夹带未声明文件；
- 签名损坏、无签名、篡改负载后不重签；
- **root 轮换**：合法 A→B（双签）、伪造轮换（缺旧 root 签名）、
  跳号（v1→v3）、v2→v1 回滚；
- **轮换中断原子性**：提交前崩溃后状态字节不变、无临时文件残留、合法包可恢复；
- HTTP 端到端：状态码、错误码、下载 404、reset。

签发端辅助代码位于 `app/repo_tool.py`，测试在其中构造合法包与各类恶意包。

---

## 7. 实际运行结果

见 [RUNLOG.md](RUNLOG.md)（记录了本次环境中真实执行的测试与 curl 演示结果，
包括被拒绝的攻击包及其错误码）。

## 8. 未完成项 / 已知简化

- 未实现 TUF 的 **mirrors/delegations**（targets 角色委派）与多仓库镜像；
  snapshot 只固定 `targets.json` 一个角色。
- 不提供持久撤销/吊销列表（real TUF 用 root 轮换解决密钥更换，本实现同样如此）。
- 原子性依赖 POSIX `rename(2)` 与 `fcntl` 文件锁；Windows 上需替换为
  `os.replace`（原子替换已兼容）与其它锁实现。
- 时间以服务器 UTC 时钟为准；没有为演示引入请求级“当前时间”头，
  过期相关单元测试通过库接口注入时间完成。
- `POST /api/reset` 无鉴权，仅适用于本地演示/测试，生产部署应移除或加保护。
- 样例仓库中的密钥为每次随机生成，**仅用于演示**。
