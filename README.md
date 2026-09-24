# 离线 SBOM 依赖核对服务

纯后端服务：解析 **CycloneDX JSON 明确子集**，按 purl / 版本 / 作用域规范化组件，
保留依赖图，使用**本地漏洞夹具**按生态规则进行**真实的版本比较**（绝不做字符串大小比较），
输出**直接 / 传递依赖**及证据路径。

> **重要边界**：漏洞数据是仓库内手工维护的小型本地夹具（`data/vulnerabilities.json`），
> **不是漏洞订阅源**，不联网、不更新，**不声称覆盖最新或全部漏洞**。
> 夹具中没有记录的包返回 `no_fixture_data`，它的含义是"无数据"，**不是"无漏洞"**。

技术栈：Python 3.10+ · FastAPI · SQLite（标准库 `sqlite3`）· Pydantic v2 · pytest。

---

## 1. 本地启动

```bash
cd P093/b
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

uvicorn app.main:app --host 127.0.0.1 --port 8000
```

数据库默认写入 `data/sbom.db`；可用环境变量覆盖：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SBOM_DB_PATH` | `data/sbom.db` | SQLite 文件路径 |
| `SBOM_FIXTURE_PATH` | `data/vulnerabilities.json` | 本地漏洞夹具路径 |

启动时会把夹具载入 SQLite 的 `advisories` / `affected_ranges` 表。

交互式文档（离线可用）：<http://127.0.0.1:8000/docs>

## 2. 验收命令

```bash
# 自动化测试（版本比较器 / purl / 循环依赖 / FastAPI 端到端）
pytest -q

# 提交示例 SBOM
curl -s -X POST http://127.0.0.1:8000/api/v1/sboms/analyze \
  -H 'Content-Type: application/json' \
  --data-binary @examples/sample-app.bom.json | python3 -m json.tool

# 已修复版本（最小示例）：affected_findings 应为 0
curl -s -X POST http://127.0.0.1:8000/api/v1/sboms/analyze \
  -H 'Content-Type: application/json' \
  --data-binary @examples/minimal.bom.json | python3 -m json.tool

# 历史扫描
curl -s http://127.0.0.1:8000/api/v1/scans | python3 -m json.tool

# 夹具声明（含"非实时源"免责说明）
curl -s http://127.0.0.1:8000/api/v1/fixture | python3 -m json.tool
```

## 3. API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/sboms/analyze` | 请求体为**原始 CycloneDX JSON**，返回扫描结果并存入 SQLite |
| `GET` | `/api/v1/scans` | 历史扫描列表 |
| `GET` | `/api/v1/scans/{scan_id}` | 某次扫描的完整结果（404 = 不存在） |
| `GET` | `/api/v1/fixture` | 本地夹具元数据与免责声明 |
| `GET` | `/healthz` | 存活检查 |

### 判定状态（findings[].status）

| 状态 | 含义 |
| --- | --- |
| `affected` | 版本经真实比较后落入某受影响范围 |
| `not_affected` | 版本可比较且不在夹具的任何受影响范围内 |
| `missing_version` | 组件/purl 无版本 → 受影响性**未知** |
| `invalid_version` | 版本字符串无法按该生态规则解析 |
| `invalid_purl` | purl 结构非法（如 `pkg:npm/`） |
| `unsupported_ecosystem` | 生态不在已实现的三套规则内（如 `pkg:golang/...`） |
| `no_fixture_data` | 本地夹具没有该包的公告；**数据缺失，不是安全结论** |
| `excluded` | 作用域为 `excluded`：保留记录但不评估 |

## 4. 支持的 CycloneDX 子集

- `bomFormat` 必须为 `CycloneDX`；支持 `specVersion` **1.4 / 1.5 / 1.6**。
- `components[]`：`type`、`bom-ref`、`name`、`version`、`purl`、`scope`
  （`required` / `optional` / `excluded`，缺省规范化为 `required`）。
- `dependencies[]`：`ref` + `dependsOn[]` 构成有向图；**循环依赖被保留**，
  未声明的 ref 进入 `warnings` 而不是静默丢弃。
- `metadata.component.bom-ref` 标识根组件；根直接指向的组件判定为**直接依赖**，
  可达的其余组件为**传递依赖**。
- **规范化与合并**：身份键为 `(purl类型, 命名空间, 名称, 版本, qualifiers, subpath, scope)`。
  - 同一身份、多个 bom-ref（重复组件）→ 合并为一个节点（`merged_count`，保留全部 bom-ref）；
  - 名称版本相同但 **qualifiers/subpath 不同（不同变体）→ 不混同**，各自独立评估；
  - `excluded` 组件仍保留在图与输出中，只是不做漏洞判定。

## 5. 版本比较规则（真实实现，禁止字符串比较）

三套比较器都在 `app/versions/`，各自先把版本**解析为结构化对象**再按数值/优先级比较：

### npm（SemVer 子集 + npm 范围语义，`semver.py`）
- 数值比较 `(major, minor, patch)`，预发布按 SemVer 2.0 优先级
  （数字标识符按数值比较，`alpha < beta < rc < release`）。
- 范围支持：比较符 `< <= > >= =`、x-range（`1`/`1.2`/`1.x`/`*`）、
  `~`、`^`（含 `^0.x` 规则）、连字符范围 `1.2.3 - 2.3.4`、空格 AND、`||` OR。
- 预发布标签规则：预发布版本只有在范围显式提到同一 `[major,minor,patch]` 时才满足。
- 端点：`>=1.2.0 <1.4.2` 含下界、不含上界，按数值判定。

### PyPI（PEP 440 子集，`pep440.py`）
- epoch / release / pre / post / dev 的真实排序（`1.0.dev1 < 1.0a1 < 1.0b1 < 1.0rc1 < 1.0 < 1.0.post1`）。
- 说明符：`== != > >= < <= ~= ===`、`==1.2.*` 通配、逗号 AND、相等时零填充（`1.4 == 1.4.0`）。
- 预发布默认不满足未显式包含预发布边界的说明符集合（PEP 440 规则）。

### Maven（`maven.py`）
- 按 Maven `ComparableVersion` 的数字/限定符 token 化与填充规则比较
  （`alpha/a < beta/b < milestone/m < rc/cr < snapshot < 发布版 < sp`，
  数字段按数值，故 `1.10 > 1.9`）。
- 范围：`[1.0,2.0)` / `(…]` 开闭端点、无界 `[2.0,)`、精确 `[1.5]`、OR 并集 `[1.0,1.5),[2.0,)`；
  裸版本按软需求处理为精确版本。

## 6. 证据路径

- 直接依赖：证据为单元素链 `["组件bom-ref"]`。
- 传递依赖：从根（或直接依赖）出发的 **bom-ref 链**（BFS 最短路，最多保留 3 条），
  例如 `["lpl-140", "tiny-110"]` 表示 `tinycrypto@1.1.0` 经直接依赖 `left-pad-lite` 引入。
- BFS 以"路径上不重复访问 ref"处理回边，因此**循环依赖不会导致死循环**；
  检测到的节点级环在 `dependency_cycles` 中作为闭合链返回。

## 7. 项目结构

```
app/
  main.py        FastAPI 路由、生命周期、请求编排
  cyclonedx.py   CycloneDX 子集解析、作用域规范化、重复合并/变体保留
  purl.py        Package URL（RFC 9259 子集）解析与规范化
  graph.py       依赖图：直接/传递判定、BFS 证据路径、环检测
  matcher.py     匹配引擎：按生态分发比较器、状态分类
  fixtures.py    本地漏洞夹具加载（带免责元数据）
  db.py          SQLite 建表、夹具装载、扫描持久化
  versions/      semver.py / pep440.py / maven.py 三套真实比较器
data/
  vulnerabilities.json   本地夹具（非实时源）
examples/
  sample-app.bom.json    综合示例（端点/预发布/循环/错误purl/缺失版本/合并/变体）
  minimal.bom.json       已修复版本最小示例
tests/           版本比较器、purl、FastAPI 端到端测试
requirements.txt / requirements-dev.txt   锁定版本（== 固定）
```

## 8. 安全相关实现说明

- **密码学操作真实执行**：每次提交的 SBOM 原始字节用 `hashlib.sha256` 计算真实 SHA-256
  （返回在 `input_sha256`，并与持久化结果一致），不存在占位哈希。
- **协议真实执行**：HTTP 经 uvicorn/ASGI 实际提供服务；purl 按 RFC 9259 语法真实解析，
  非法 purl 抛错而不是"猜测修正"。
- **比较真实执行**：无任何版本字符串字典序比较；无法解析时返回 `invalid_version` / `missing_version`，
  不臆测受影响性。
- 夹具元数据 `live_feed=false` 与免责文案随每次分析响应（`fixture` 字段）返回。
