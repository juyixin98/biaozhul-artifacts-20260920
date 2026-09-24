# SBOM 风险匹配服务（纯后端）

一个基于 **Python + FastAPI + cryptography** 的软件物料（SBOM）风险匹配服务。
输入自定义 JSON 格式的 SBOM，使用受限的 **SemVer 2.0 区间语法** 将物料与一份
**带 Ed25519 分离签名的本地漏洞馈送（夹具）** 匹配，输出：

- 每个受影响 / 不受影响 / 未知的包-漏洞结论；
- 从根组件到受影响（或未知）包的**全部依赖路径**；
- 依赖图中的**传递依赖环**（多点环 + 自环）；
- 同名包的**不同版本并存**、同名包的**不同生态严格隔离**。

纯后端，无界面。交互式 API 文档由 FastAPI 内置（`/docs`、`/openapi.json`）。

## 目录结构

```
sbom_risk/
  semver.py    # SemVer 2.0.0 解析与优先级（含预发布数字标识前导零校验）
  ranges.py    # 自定义区间语法：= != > >= < <=、^、~、*、AND 组合
  models.py    # SBOM 自定义 JSON 的 pydantic 模型；包身份键 eco:name@version
  graph.py     # 依赖图构建、Tarjan SCC 环检测、根→目标简单路径枚举
  feed.py      # 漏洞馈送信封加载 + Ed25519 验签（cryptography）
  matching.py  # 风险匹配主逻辑 / 报告结构
  app.py       # FastAPI 应用与 HTTP 接口
scripts/
  gen_test_keys.py  # 生成夹具用 Ed25519 密钥对
  sign_feed.py      # 用私钥对馈送 payload 做规范 JSON 分离签名
  run_dev.sh        # 本地启动
fixtures/
  vuln-feed.payload.json   # 漏洞夹具原文
  vuln-feed.json           # 已签名信封（服务实际加载）
  sbom.acceptance.json     # 验收 SBOM：环 / 多版本 / 边界 / 未知 / 跨生态
  keys/                    # 仅本地夹具使用的测试密钥（勿用于生产）
examples/
  sbom.minimal.json        # 最小请求体
  curl_examples.sh         # curl 请求样例
tests/                     # pytest 自动化测试（120 个用例）
requirements.txt           # 锁定依赖
requirements.in            # 顶层直接依赖
```

## 环境与依赖

- Python **3.10+**（开发与实测在 Python 3.12.3 / Linux x86_64）。
- 直接依赖：`fastapi`、`uvicorn[standard]`、`cryptography`、`pydantic`；
  测试用 `pytest`、`httpx`。完整版本见 `requirements.txt`（已锁定）。

## 安装与启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 启动（默认 127.0.0.1:8000，可用 SBOM_HOST / SBOM_PORT 覆盖）
./scripts/run_dev.sh
# 或直接：
# python -m uvicorn sbom_risk.app:app --host 127.0.0.1 --port 8000
```

服务启动时会加载并验证漏洞馈送；**签名不通过会直接启动失败**（拒绝静默运行无情报服务）。

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `SBOM_FEED_PATH` | `fixtures/vuln-feed.json` | 已签名馈送信封路径 |
| `SBOM_PUBLIC_KEY_PATH` | `fixtures/keys/test_feed_public.pem` | Ed25519 公钥 PEM |
| `SBOM_ALLOW_UNSIGNED` | 未设置 | `1/true` 时允许无公钥加载未签名馈送（仅调试） |

## 运行测试

```bash
source .venv/bin/activate
python -m pytest tests/ -q
```

## HTTP 接口

### `GET /healthz`
健康检查，返回服务版本与馈送验签状态。

```bash
curl http://127.0.0.1:8000/healthz
# {"status":"ok","service_version":"1.0.0","feed_verified":true}
```

### `GET /api/v1/feed/info`
列出当前馈送元数据、验签状态与全部漏洞条目（含区间）。

### `POST /api/v1/match`
提交 SBOM，返回匹配报告。

请求体（自定义 JSON）：

```json
{
  "sbom_version": "1.0",
  "component": {"name": "demo-app", "version": "0.1.0"},
  "packages": [
    {"ecosystem": "npm", "name": "demo-app", "version": "0.1.0",
     "is_root": true, "dependencies": ["npm:left-pad@1.3.0"]},
    {"ecosystem": "npm", "name": "left-pad", "version": "1.3.0",
     "dependencies": []}
  ]
}
```

字段约定：

- `ecosystem` + `name` + `version` 构成包身份键 `ecosystem:name@version`。
  **同名不同生态是不同包；同名不同版本是不同节点**，绝不合并。
- `dependencies` 元素必须是已存在的完整包身份键；悬空引用返回 `422`。
- `is_root` 可省略：没有任何显式根时，以入度为 0 的节点为根。
- `version` 必须是 SemVer 2.0.0；无法解析的版本对相关漏洞给 `unknown`。

响应（节选）：

```json
{
  "feed": {"feed_version": "2026.09.24", "verified": true, "...": "..."},
  "summary": {
    "packages_total": 2,
    "packages_matching_advisory": 1,
    "affected_packages": 1,
    "affected_findings": 1,
    "not_affected_packages": 0,
    "unknown_findings": 0,
    "cycles_count": 0
  },
  "roots": ["npm:demo-app@0.1.0"],
  "cycles": [],
  "findings": [
    {
      "package": {"id": "npm:left-pad@1.3.0", "ecosystem": "npm",
                  "name": "left-pad", "version": "1.3.0"},
      "vulnerability": {"id": "VULN-LEFTPAD-001", "severity": "high", "...": "..."},
      "paths": [["npm:demo-app@0.1.0", "npm:left-pad@1.3.0"]],
      "reachable_from_roots": true,
      "paths_truncated": false,
      "status": "affected",
      "matched_ranges": [">=1.0.0 <2.0.0"]
    }
  ]
}
```

- `status`：`affected`（命中）/ 不出现在 findings 即 `not_affected` /
  `unknown`（版本无法解析，或漏洞条目未给出区间）。
- `paths`：从每个根到该包的所有**简单路径**（环上节点不重复进入）；
  不可达时为空数组且 `reachable_from_roots=false`。
- 路径枚举设有保护上限（单目标 50 条、单路径 40 节点），触顶时
  `paths_truncated=true`。

错误：

- SBOM 结构不合法（pydantic 校验失败）→ `422`；
- 依赖悬空 / 重复节点等图错误 → `422`，body 形如
  `{"error":"invalid_sbom_graph","detail":"..."}`。

更多请求样例见 `examples/curl_examples.sh`（服务启动后直接运行）。

## SemVer 区间语法（受限子集）

| 写法 | 含义 |
|---|---|
| `1.2.3` / `=1.2.3` | 精确相等 |
| `!=1.2.3` | 不等 |
| `> >= < <=` | 数值比较，可自由 AND 组合 |
| `>=1.0.0 <2.0.0` | AND 交集（分隔符：空白 / `,` / `&&`） |
| `^1.2.3` | `>=1.2.3 <2.0.0`（0.x 锁次版本，0.0.x 锁修订，同 npm） |
| `~3.1.0` | `>=3.1.0 <3.2.0`（锁主.次版本） |
| `*` | 任意**稳定**版本 |

明确**不支持**：OR（`||`）、x-range（`1.2.x`）、连字符区间（`1.0 - 2.0`）、
缩写（`~1`、`~1.2`）、操作符与版本间的空格。一条漏洞需要不相交区间时，
在漏洞记录的 `ranges` 数组里写多个元素（元素间为 OR，元素内为 AND）。

**预发布策略（安全优先，已在测试中固定）：**

1. 候选为稳定版：纯数值比较。
2. 候选为预发布版：除数值比较外，必须至少有一个比较器通过“预发布门”——
   即该比较器参照版本与候选共享同一 `major.minor.patch` 核心，且是
   精确相等、`^/~` 展开边界，或参照版本本身带预发布段。
   因此 `2.0.0-alpha` **不**满足 `>=1.0.0 <2.0.0`，但满足
   `>=1.0.0-alpha <2.0.0`；`1.2.0-beta.5` 满足 `>=1.2.0-alpha <1.2.0`。
3. `*` 永不匹配预发布版本。

## 馈送签名

`fixtures/vuln-feed.json` 是信封：`{alg, kid, signature_b64, payload}`。
签名内容为 payload 的**规范 JSON**（键排序、紧凑分隔、UTF-8、不转义非 ASCII），
用 Ed25519 签名（64 字节，base64）。验签使用
`cryptography.hazmat.primitives.asymmetric.ed25519`。

重新生成夹具：

```bash
python -m scripts.gen_test_keys      # 仅首次：生成密钥对
python -m scripts.sign_feed \
  fixtures/vuln-feed.payload.json \
  fixtures/keys/test_feed_private.pem \
  fixtures/vuln-feed.json
```

篡改 payload 一个字节（例如把区间改成 `*`）即会被验签拒绝，
对应测试：`tests/test_feed.py::test_tampered_payload_rejected`。

> `fixtures/keys/` 下的私钥**只用于本地夹具签名**，不是生产密钥，
> 也不应被任何真实信任链引用。

## 验收点与实测结果（2026-09-24 实跑记录）

验收夹具：`fixtures/sbom.acceptance.json`（33 个包节点，8 条漏洞情报）。
以下结果由真实运行得到，非推断。

**1. 传递依赖环**（Tarjan SCC）：检测到 2 个环：

- 二节点环：`cycle-a@1.5.0 ↔ cycle-b@1.9.9`；
- 自环：`selfish@1.0.0 → selfish@1.0.0`。

路径枚举穿越环时只走简单路径，不会无限绕环
（`tests/test_graph.py::test_paths_through_cycle_are_simple`）。

**2. 多个版本并存 + 受影响路径**：`left-pad` 同时存在
`0.9.0 / 1.3.0 / 2.0.0 / 2.0.0-alpha / 1.0(非法)` 五个节点；
其中 `left-pad@1.3.0` 命中 `VULN-LEFTPAD-001`（区间 `>=1.0.0 <2.0.0`），
并枚举出 **2 条根路径**：

```
app-root@1.0.0 → left-pad@1.3.0
app-root@1.0.0 → mid-helper@2.0.0 → left-pad@1.3.0
```

**3. 区间边界**（`VULN-MULTI-7`：`~3.1.0` 或 `>=3.2.0 <=3.2.1`）：

| 版本 | 3.0.9 | 3.1.0 | 3.1.9 | 3.2.0 | 3.2.1 | 3.2.2 |
|---|---|---|---|---|---|---|
| 结论 | 不受影响 | 命中（下界含） | 命中 | 命中 | **命中（闭区间上界含）** | 不受影响 |

预发布边界（`VULN-PRE-42`：`>=1.2.0-alpha <1.2.0` 或 `=2.0.0-rc.1`）：
`1.2.0-alpha` / `1.2.0-beta.5` 命中，`1.1.9`、稳定版 `1.2.0`、`2.0.0-rc.2`
不命中。

**4. 未知版本状态**（3 个 `unknown` finding）：

- `npm:left-pad@1.0` —— `invalid_semver`（`1.0` 不是 SemVer）；
- `npm:weird-x@01.2.3` —— `invalid_semver`（前导零非法）；
- `npm:no-range-lib@5.5.5` —— `vulnerability_without_range`（情报缺区间）。

`*` 区间命中稳定版 `weird-x@3.0.0`，但不命中预发布
`weird-x@4.0.0-beta.1`。

**5. 同名不同生态不混淆**：馈送只有 `pypi:json` 情报。同时提交
`npm:json@1.5.0` 与 `pypi:json@1.5.0` 时，只有后者报 `affected`，
前者完全不参与该漏洞评估。

**测试运行结果**：

```
$ python -m pytest tests/ -q
120 passed, 1 warning in 0.17s
```

唯一 warning 为 starlette TestClient 对 `httpx` 的弃用提示，不影响功能。

**实跑 HTTP 验证**（`uvicorn sbom_risk.app:app` 实际起服后用 curl）：
`/healthz` 返回 `feed_verified:true`；最小 SBOM 与验收 SBOM 的
`/api/v1/match` 结果与上表一致；悬空依赖返回 `422`；`/docs` 与
`/openapi.json` 返回 200。

## 未完成项 / 已知限制（如实记录）

1. **无持久化 / 多租户 / 鉴权**：馈送在进程启动时一次性加载，更新需重启；
   接口无认证，仅适合本地或受控网络。
2. **漏洞情报为本地夹具**：不接入 OSV/NVD 等在线源，无增量更新与撤销机制。
3. **区间语法是受限子集**：不支持 OR 内联、x-range、连字符区间、
   `1.2` 这类两段版本；生态间版本规则差异（如 npm 预发布惯例 vs Maven）
   未做差异化处理，统一按 SemVer 2.0.0。
4. **非 SemVer 版本一律 `unknown`**：不会尝试部分匹配或宽松解析（这是有意的
   保守策略，但意味着 `1.0`、`v1.2.3` 等都无法判定）。
5. **路径枚举上限**：单目标 50 条路径 / 单路径 40 节点，超限时
   `paths_truncated=true`（结论仍完整，仅展示路径可能不全）。
6. **性能**：按每个漏洞-包配对独立评估、路径 DFS 枚举，夹具规模足够；
   超大型 SBOM（数万节点）未做基准与优化。
7. **测试密钥随仓库分发**：仅限夹具用途；真实部署必须替换密钥与运维流程。
8. CI 配置（如 GitHub Actions）未提供；测试在本地 Python 3.12 实跑通过。
