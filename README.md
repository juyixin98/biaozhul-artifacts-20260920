# 离线 SBOM 依赖核对服务（Offline SBOM Dependency Matcher）

纯后端服务：上传 **CycloneDX JSON** 的明确子集，按 **purl / 版本 / 作用域（scope）**
规范化组件、保留依赖图，再与**本地漏洞夹具**按**各生态自己的版本规则**核对，
输出受影响结论与**传递依赖证据路径**。

- 语言/框架：Python 3.12 · FastAPI · SQLite（标准库 `sqlite3`）
- 无外网依赖；漏洞数据全部来自仓库内的合成夹具 `fixtures/vulnerabilities.json`

> ⚠️ **重要限制（如实声明）**
> 漏洞数据是随仓库的**本地合成夹具**，条目编号与描述均为虚构，**仅用于离线
> 演示与自动化测试**。它不是实时漏洞情报源，**不声称也不覆盖最新漏洞**。
> 生产用途需替换为可信的真实情报源。

---

## 1. 支持的范围（明确子集）

**CycloneDX**（`bomFormat: "CycloneDX"`，`specVersion` 1.4 / 1.5 / 1.6）：

- `metadata.component`：根（直接）组件；
- `components[]`：`bom-ref`、`type`、`name`、`version`、`scope`、顶层 `purl`；
- `dependencies[]`：`ref` + `dependsOn[]` 的 bom-ref 邻接表（依赖图原样保留）。

文档不在子集内（非 CycloneDX、版本不符、结构错误）→ **HTTP 422**，明确报错，
不做静默猜测。

**生态与版本范围语法**（每种都按该生态的真实算法解析，**不做字符串大小比较**）：

| 生态 (purl type) | 版本顺序 | 范围语法（夹具 `version_range`） |
|---|---|---|
| `npm` | SemVer 2.0 | `^` / `~` / `x` 范围 / `1.2.3 - 2.3.4` / 比较器交集 / `||` 并集；含 npm 预发布门禁 |
| `maven` | Maven 版本顺序（alpha<beta<milestone<rc<snapshot<release<sp） | `[1.0,2.0)`、`[1.0,2.0]`、`(,3.0]`、精确版本 |
| `pypi` | PEP 440（`packaging`） | `>=,<,<=,>,==,!=,~=` |
| `gem` | RubyGems 分段顺序 | `>=,<,=,!=,~>`（pessimistic） |
| `deb` | dpkg 顺序（epoch、`~`、字母/非字母序） | `>=,<,=,...` 比较器集合（逗号交集） |

其它生态（如 `cargo`、`golang`）可正常解析组件，但没有比较器，结论为
**未知（unsupported_ecosystem）**，绝不误判为受影响或不受影响。

**未知（unknown）而非猜测**：组件 purl 缺失版本 → `missing_version`；
版本/范围无法按生态规则解析 → `invalid_version`；生态无比较器 →
`unsupported_ecosystem`。

**合并与变体**：同一规范化 purl（含版本）重复出现 → 合并为一个节点
（scope 取 `required` 优先）；不同版本或不同 qualifiers（如
`?os=linux` vs `?os=darwin`）是**不同变体，绝不混同**。

---

## 2. 目录结构

```
app/
  purl.py        # purl 解析与规范化（显式实现，错误输入抛异常）
  versions.py    # npm/maven/pypi/gem/deb 的真实版本解析与范围比较
  sbom.py        # CycloneDX 子集解析、组件合并、依赖图与 Tarjan 环检测
  engine.py      # 漏洞匹配、未知语义、传递依赖最短证据路径（BFS，循环安全）
  db.py          # SQLite 持久层（运行记录 + 组件结论）
  service.py     # 组装 + 请求体 SHA-256 真实计算
  main.py        # FastAPI 路由
fixtures/
  vulnerabilities.json   # 本地合成漏洞夹具（虚构，非最新漏洞）
examples/
  sbom-full.json          # 覆盖预发布/端点/循环/错误purl/重复/变体
  sbom-minimal.json
tests/                    # pytest：purl、版本算法、引擎/图、HTTP API
scripts/acceptance.sh     # 一键端到端验收
requirements.txt          # 直接依赖
requirements.lock         # 完整锁定（pip freeze）
```

---

## 3. 本地启动

```bash
cd /path/to/P093/a
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock     # 安装锁定依赖
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8791
```

SQLite 数据库在首次写入时创建于 `data/sbom.db`（已被 `.gitignore` 忽略）。

快速验证：

```bash
curl -s http://127.0.0.1:8791/health | .venv/bin/python -m json.tool
curl -s http://127.0.0.1:8791/vulnerabilities | .venv/bin/python -m json.tool
```

---

## 4. API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/health` | 服务状态、支持生态、夹具来源与“非最新漏洞”声明 |
| GET  | `/vulnerabilities` | 列出本地夹具条目（标注为虚构） |
| POST | `/api/v1/sbom/check` | 上传 CycloneDX JSON（请求体原样接收），返回核对结果并落库 |
| GET  | `/api/v1/runs?disposition=affected\|unknown\|not_affected&limit=` | 列出历史运行，可按结论过滤 |
| GET  | `/api/v1/runs/{id}` | 取回某次运行的完整结果（404 = 不存在） |

状态码：`200` 成功；`400` 空体/非法 JSON；`404` 运行不存在；
`422` SBOM 不在支持子集。

核对请求：

```bash
curl -s -X POST http://127.0.0.1:8791/api/v1/sbom/check \
  -H 'content-type: application/json' \
  --data-binary @examples/sbom-full.json
```

返回要点：

- `summary`：组件总数、affected/unknown/not_affected 计数、环数、合并数、忽略数；
- `results.affected[]`：`matched`（命中的漏洞 id / 范围 / 修复版本）、
  `is_transitive`、`evidence_paths`（根 → 目标的最短 purl 链，菱形依赖给多条）；
- `results.unknown[]`：`reason_code`（`missing_version` 等）+ 人类可读原因；
- `dependency_graph`：节点、边、根、**循环 SCC**；
- `ignored_components[]`：非法 purl / 缺版本等被跳过项及原因；
- `run`：运行 id、UTC 时间、**原始请求体的 SHA-256**（`hashlib` 真实计算）；
- `data_source.not_real_advisory=true` 与“不覆盖最新漏洞”声明。

---

## 5. 自动化测试

```bash
.venv/bin/python -m pytest -q
```

覆盖（103 个用例）：

- **预发布版本**：npm 预发布门禁、SemVer/PEP440/RubyGems/Maven 的 rc 顺序；
- **范围端点**：开/闭区间、连字符区间、`~>` pessimistic、dpkg `~` 与 epoch；
- **非字符串比较**：`10` vs `9`、`7.0` vs `7.0.0` 等结构化顺序；
  Debian 比较器额外与系统 `dpkg --compare-versions` 参考实现交叉验证；
- **循环依赖**：A↔B 环、自环（Tarjan 检测 + BFS 路径搜索不挂死）；
- **错误 purl / 缺版本**：进入 `ignored` 或 `unknown` 并如实报告；
- **重复合并 / 变体不混同**：含平台专属漏洞只命中对应变体；
- **HTTP 层**：状态码、持久化与筛选、SHA-256 与原始字节一致。

---

## 6. 一键验收

启动/健康/核对/哈希/关键结论全自动校验（会用临时全新数据库并在结束时停服）：

```bash
./scripts/acceptance.sh 8791
```

成功时最后一行输出 `ACCEPTANCE PASSED`。它断言了：环数=1、合并数=1、
未知=1、Maven 修复端点不受影响、npm 预发布门禁、PEP 440 rc 顺序、
linux 专属漏洞不污染其它变体、curl 传递证据路径、cargo→未知、
非法 purl/缺版本被报告，以及 SHA-256 与 `sha256sum` 完全一致。

---

## 7. 设计说明与诚实边界

- **版本比较是真实算法**：SemVer/Maven/RubyGems/dpkg 在此显式实现并以解析后的
  结构化数字段比较；PEP 440 使用 `packaging` 的实现；没有任何
  `str` 大小比较。无法解析时返回**未知**，不回退到猜测。
- **证据路径**：在保留的依赖图上做逐层 BFS，输出从直接（根）组件到受影响组件的
  最短 purl 链；菱形依赖会给出多条等长路径。搜索对环安全。
- **失败如实报告**：SBOM 不支持 → 422；JSON 非法/空体 → 400；
  组件级问题进入 `ignored_components` / `unknown`，不静默丢弃；
  持久化失败 → 500。
- **密码操作**：每次运行对**原始请求字节**计算 SHA-256（标准库 `hashlib`，
  真实执行），并在测试中与外部 `sha256sum` 交叉核对。
- **非目标**：无前端、无在线漏洞拉取、无许可证分析；夹具为虚构数据，
  **不覆盖最新漏洞**。
