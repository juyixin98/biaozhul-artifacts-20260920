# sbom-risk-matcher

SBOM 风险匹配服务（纯后端）：接收自定义 JSON 格式的 SBOM，用有界的 SemVer 范围语法
与本地漏洞夹具匹配，报告受影响组件、从根组件出发的依赖路径、依赖环与未知版本状态。
同名不同生态的包（如 npm 与 PyPI 的 `acme-net`）视为完全不同的包，绝不合并。

技术栈：Python 3.12 · FastAPI · Pydantic v2 · cryptography（报告摘要与 HMAC 签名）。

## 目录结构

```
app/
  semver.py    # SemVer 版本比较 + 范围语法解析（无第三方依赖）
  models.py    # 自定义 SBOM / 漏洞条目的 Pydantic 模型
  matcher.py   # 依赖图构建、环检测、路径枚举、匹配主逻辑
  crypto.py    # SHA-256 摘要与 HMAC-SHA256 报告签名（cryptography）
  main.py      # FastAPI 入口与路由
fixtures/vulnerabilities.json   # 本地漏洞夹具（5 条虚构漏洞）
examples/sbom_cycle.json        # 示例：传递依赖环 root→a→b→c→a，c→deep-vuln
examples/sbom_boundaries.json   # 示例：多版本、区间边界、预发布、跨生态同名、未知版本
tests/                          # pytest 自动化测试（65 个用例）
requirements.txt / requirements-dev.txt / requirements.lock
```

## 安装与启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # 或 requirements.lock 获得完全锁定的版本
.venv/bin/uvicorn app.main:app --port 8000
```

运行测试：

```bash
.venv/bin/python -m pytest -q
```

可选环境变量：`SBOM_HMAC_KEY` —— 报告 HMAC 签名密钥，缺省为仅供本地开发的弱密钥。

## 自定义 JSON 格式

### SBOM

```json
{
  "format": "sbom-matcher/1",
  "name": "my-app",
  "components": [
    {
      "id": "root",
      "name": "my-app",
      "ecosystem": "npm",
      "version": "1.0.0",
      "dependencies": ["lib-a"]
    },
    {"id": "lib-a", "name": "acme-net", "ecosystem": "npm", "version": "1.4.1"}
  ]
}
```

- `id`：SBOM 内唯一，依赖边通过 id 引用；允许成环。
- `ecosystem`：生态名（npm、pypi、maven……），匹配时大小写不敏感。
- `version`：必须是完整 SemVer（`X.Y.Z`，可带 `-prerelease` / `+build`）。
  为 `null` 或无法解析时，该组件状态为 **unknown**，不判受影响。

### 漏洞条目

```json
{
  "id": "VULN-2026-0001",
  "package": {"ecosystem": "npm", "name": "acme-net"},
  "summary": "……",
  "ranges": [">=1.0.0 <1.4.2"],
  "fixed_versions": ["1.4.2"]
}
```

`ranges` 数组内为并集（任一命中即受影响）。

## SemVer 范围语法（有界子集）

| 语法 | 含义 |
|---|---|
| `1.2.3` / `=1.2.3` | 精确匹配 |
| `> >= < <= 1.2.3` | 比较符（可配部分版本，如 `>=1.2` 即 `>=1.2.0`，`<=1.2` 即 `<1.3.0`） |
| `1.2` `1` `1.2.x` `*` | 部分版本 / 通配符 |
| `^1.2.3` | `>=1.2.3 <2.0.0`（`^0.2.3` → `<0.3.0`，`^0.0.3` → `<0.0.4`） |
| `~1.2.3` | `>=1.2.3 <1.3.0` |
| `>=1.0.0 <2.0.0` | 空格或逗号分隔的交集 |
| `a \|\| b` | 并集 |

预发布规则（与 npm semver 一致）：仅当比较符集合中存在相同 `[major, minor, patch]`
且带预发布标识的比较符时，预发布版本才可命中。例如 `>=2.0.0-alpha <2.0.0` 命中
`2.0.0-rc.1`，而 `<2.0.0` 不命中任何 `2.0.0-x`。

明确不支持：连字符范围 `1.2.3 - 2.0.0`（解析时报错并在响应 `warnings` 中说明）。

## HTTP 接口

- `GET /health` → `{"status": "ok"}`
- `GET /v1/vulnerabilities` → 内置漏洞夹具及其 SHA-256 摘要
- `POST /v1/match` → 匹配报告。请求体 `{"sbom": {...}, "vulnerabilities": [...]?}`，
  缺省使用内置夹具。响应含：
  - `matches[]`：每个命中含 `dependency_paths`（从根到受影响组件的简单路径，
    有环保护，单命中最多返回 50 条，超出时 `dependency_paths_truncated=true`）
  - `unknown[]`：版本缺失/非法的组件及其可能相关的漏洞 id
  - `cycles[]`：检测到的依赖环（旋转归一去重）
  - `sbom_digest`：SBOM 规范 JSON 的 SHA-256
  - `signature`：整份报告的 HMAC-SHA256（密钥见 `SBOM_HMAC_KEY`）

### 请求样例

```bash
# 依赖环示例（使用内置夹具）
curl -s -X POST http://127.0.0.1:8000/v1/match \
  -H 'Content-Type: application/json' \
  -d "{\"sbom\": $(cat examples/sbom_cycle.json)}"

# 自带漏洞数据覆盖夹具
curl -s -X POST http://127.0.0.1:8000/v1/match \
  -H 'Content-Type: application/json' \
  -d '{
        "sbom": {"format": "sbom-matcher/1", "components": [
          {"id": "root", "name": "app", "ecosystem": "npm", "version": "1.0.0"}
        ]},
        "vulnerabilities": [
          {"id": "X-1", "package": {"ecosystem": "npm", "name": "app"},
           "ranges": ["<2.0.0"]}
        ]
      }'
```

## 实测结果（2026-09-24，Python 3.12.3）

- `pytest -q`：**65 passed**（1 条无害警告：starlette TestClient 提示未来将改用 httpx2）。
- `POST /v1/match` + `examples/sbom_cycle.json`：检出环 `[a, b, c]`，`deep-vuln@2.1.0`
  命中 `VULN-2026-0005`，路径 `root→a→b→c→dv`，环未造成死循环。
- `POST /v1/match` + `examples/sbom_boundaries.json`：`affected=4, unknown=2`——
  - `acme-net@1.4.1`(npm) 命中、同包 `@1.4.2` 不命中（多版本区分）；
  - `acme-net@0.8.3`(pypi) 只命中 pypi 漏洞 `VULN-2026-0002`，未混入 npm 漏洞（生态隔离）；
  - `libtrans@2.0.0-rc.1` 命中预发布范围，正式版 `2.0.0` 不命中；
  - `boundary-lib@1.0.0` 命中（下界包含）、`@2.0.0` 不命中（上界排除）；
  - `version: null` → `unknown/missing_version`，`version: "latest"` → `unknown/invalid_semver`。

## 已知限制 / 未完成项

- 漏洞数据仅为本地静态夹具，未对接 OSV/NVD 等真实数据源，无增量更新机制。
- 范围语法为刻意有界的子集：不支持连字符范围、不支持 `!=`。
- 包名匹配为精确字符串（生态不区分大小写），未做各生态的名称归一化
  （如 PyPI 的 `-`/`_` 等价、npm scope 规则）。
- 路径枚举按组件 id 去重（简单路径），在超大规模稠密图上靠 50 条上限截断，
  未提供"最短路径优先"之外的排序策略。
- HMAC 签名为对称方案，密钥分发由部署方负责；未实现非对称签名与密钥轮换。
- 服务无认证与限流，仅适合内网/本地部署。
