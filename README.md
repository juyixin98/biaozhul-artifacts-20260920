# 依赖锁一致性门禁 (Dependency Lock Consistency Gate)

一个**纯后端、完全离线**的 Node.js 项目依赖锁校验服务（Python + FastAPI）。
它接收 `package.json` 与**且仅一个** `package-lock.json`（限定
`lockfileVersion: 3`），在**不联网、不执行任何安装脚本**的前提下，回答一个问题：

> 这个锁文件描述的依赖，凭这份输入包自身，是否被**密码学地、完整地**证明可复现？

**核心立场：存在 lock 文件 ≠ 可复现。** 一个只有 `package-lock.json` 却没有
（或哈希对不上）制品 tarball 的项目，永远拿不到 `reproducibility.status = true`。

---

## 它真正校验什么

| 维度 | 检查 |
| --- | --- |
| 根依赖范围 | 解析真实的 npm semver 子集（`^ ~ > >= < <= - \|\| x-ranges`、hyphen、prerelease 规则），核对 manifest 声明范围与锁中**解析版本** |
| 锁内一致 | root/每个 workspace 的 importer 记录与 manifest 的键集合、spec 字符串逐字一致（漂移即报） |
| 解析关系 | 按真实 npm `node_modules` 逐级向上查找算法定位每条边的目标；缺失传递依赖即报错 |
| 完整性字段 | 解析 `integrity`（SRI 形式），用 `hashlib` **重新计算** sha512/sha384/sha256，`hmac.compare_digest` 常量时间比对；sha1 标记为弱算法 |
| 制品内容 | vendored tarball 是真实 gzip tar；读取其中 `package/package.json` 交叉核对 name/version；**脚本只读取、绝不执行** |
| 工作区 | `workspaces` glob 真实展开建模，`link` 节点折叠到本地 workspace 目标；`workspace:` 协议单独校验 |
| 可选/平台依赖 | `optionalDependencies`、`optional` 标记、`peerDependenciesMeta.optional`、`os/cpu/libc` 正负约束分别建模，按当前平台判定 |
| 不支持语法 | `file:` `link:` `git:` `github:` `http(s):` `npm:` alias、`portal:`、`overrides`、dist-tag `latest`、yarn/pnpm 锁、非 v3 版本 —— **明确拒绝，绝不猜测放行** |
| 图健康 | Tarjan SCC 检测循环依赖、重复版本（相同内容=告警；相同版本不同完整性=错误）、不可达冗余节点 |

输出包含：

* **最短问题链**（每个发现都带从项目根到问题节点的 BFS 最短依赖路径）；
* **可复核差异**（`diff`：value/range/file/presence，附 `unified_diff` 文本、期望值/实际值、支撑文件路径）。

---

## 安全边界（检查输入包内路径，禁止运行安装脚本）

* 归档类型由**魔数**判定，不信客户端文件名；
* 每个条目路径规范化后必须留在解包根内：拦截绝对路径、`..` 穿越、反斜杠、
  Windows 盘符、UNC（zip-slip / tar-slip）；
* tar 的符号链接/硬链接/设备节点、zip 的 Unix symlink 一律拒绝；
* 成员数、单文件大小、解压总量、压缩比均有上限（解压炸弹防护）；
* JSON 使用 duplicate-key 拒绝解析器（重复键不会被静默覆盖）；
* 全程在内存中处理，**文件系统不落盘、不 spawn 子进程、不运行
  preinstall/install/postinstall**。带安装钩子的包会产生
  `INSTALL_SCRIPT_PRESENT` 告警并使可复现性为 false。

---

## 目录结构

```
app/
  semver.py      自包含、真实的 npm semver 范围解析器（不支持的语法显式抛错）
  extract.py     安全解包（魔数/路径穿越/链接/炸弹/重复键）
  integrity.py   真实 sha512/384/256 计算与比对、tarball 清单只读检查
  workspace.py   workspaces glob 建模
  verifier.py    核心校验引擎（图遍历、范围、平台、循环、最短链、差异）
  models.py      Pydantic 报告契约
  api.py         FastAPI（/healthz、/api/v1/verify、/api/v1/verify/files）
  cli.py         命令行入口
tests/           111 个自动化测试（真实 tarball + 真实哈希，无 mock）
examples/        7 个可直接验收的真实 bundle
scripts/         示例构建脚本
wheels/          离线安装用的精确 wheel（与 requirements.lock 哈希对应）
requirements.lock  带 --hash 的完整锁定依赖（运行时 + 测试）
```

---

## 本地启动

需要 Python 3.12。

### 方式 A：直接用现有环境

```bash
pip install -r requirements.txt          # 或 fastapi/uvicorn/pydantic/python-multipart 已就绪
uvicorn app.api:app --host 127.0.0.1 --port 8000
```

### 方式 B：用哈希锁定文件做可复现离线安装（推荐验收）

```bash
python -m venv .venv
. .venv/bin/activate
pip install --require-hashes --no-index --find-links=wheels -r requirements.lock
uvicorn app.api:app --host 127.0.0.1 --port 8000
```

`requirements.lock` 里每个 sha256 都是对应 wheel 的**真实摘要**；
`--require-hashes` 会让 pip 拒绝任何哈希不符的包。

---

## API

### `GET /healthz`

### `POST /api/v1/verify`（multipart 上传一个 `.tgz/.tar.gz/.zip`）

查询参数：`os`（默认 linux）、`cpu`（默认 x64）、`libc`（默认 glibc，可空）、
`production`（排除 devDependencies）、`require_vendored_tarballs`
（缺失 vendored tarball 升级为硬错误）。

HTTP 状态始终是 200（归档本身非法才是 400）；**门禁结论看响应体的 `status`**。

```bash
curl -s -X POST "http://127.0.0.1:8000/api/v1/verify?require_vendored_tarballs=true" \
  -F "file=@examples/01-happy-reproducible.tgz" | jq '.status, .reproducibility'
```

`POST /api/v1/verify/files` 接受按路径命名的多个独立文件（内容相同的校验逻辑）。
交互式 OpenAPI 文档在 `http://127.0.0.1:8000/docs`。

---

## 命令行

```bash
python -m app.cli verify examples/03-lock-drift.tgz --pretty
# 退出码：0 通过；1 门禁失败；2 输入包非法
```

---

## 验收命令

```bash
# 1) 全量自动化测试（111 个）
python -m pytest -q

# 2) 七个示例的端到端结果
for f in examples/*.tgz; do
  echo "== $f"; python -m app.cli verify "$f" \
    | python3 -c 'import json,sys;r=json.load(sys.stdin);print(r["status"], r["reproducibility"]["status"], [x["code"] for x in r["findings"]])'
done
```

预期：

| 示例 | status | reproducible | 关键发现 |
| --- | --- | --- | --- |
| 01-happy-reproducible | pass | **true** | — |
| 02-platform-optional（linux/x64） | pass | true | `PLATFORM_EXCLUDED_OPTIONAL`(info) |
| 03-lock-drift | fail | false | `LOCK_RANGE_DRIFT` |
| 04-missing-integrity | fail | false | `MISSING_INTEGRITY` |
| 05-circular-dependency | pass | true | `DEPENDENCY_CYCLE`(warning) |
| 08-duplicate-versions | pass | true | `DUPLICATE_VERSION_COPIES`(warning) |
| 06-tampered-tarball | fail | false | `TARBALL_INTEGRITY_MISMATCH`（真实哈希不符） |
| 07-unsupported-syntax | fail | false | `UNSUPPORTED_RANGE`（github: 被拒绝） |

平台可选项在匹配平台上会被真实校验：

```bash
python -m app.cli verify examples/02-platform-optional.tgz --os darwin --cpu arm64
# -> vendored_tarballs_verified = 1，无 PLATFORM_EXCLUDED 信息
```

证明“有 lock 不等于可复现”：

```bash
# 只给锁、不带制品时，require_vendored 下直接失败；默认模式下 status 可 pass，
# 但 reproducibility.status 恒为 false
python -m app.cli verify examples/03-lock-drift.tgz   # 锁漂移，fail
```

重新生成示例（真实 gzip + 真实 sha512）：

```bash
python scripts/build_examples.py
```

---

## 报告示例（节选）

```json
{
  "status": "fail",
  "summary": {"vendored_tarballs_verified": 0, "errors": 1},
  "reproducibility": {"status": false, "reasons": ["one or more blocking findings above"]},
  "findings": [
    {
      "code": "LOCK_RANGE_DRIFT",
      "severity": "error",
      "subject": "node_modules/left-pad",
      "message": "locked left-pad@2.0.0 does not satisfy the declared range '^1.3.0' required from <root>",
      "chain": ["<root>", "--left-pad '^1.3.0'--> left-pad@2.0.0"],
      "diff": {"kind": "range", "expected": "^1.3.0", "actual": "left-pad@2.0.0", "unified": "--- expected ..."}
    }
  ]
}
```

---

## 设计取舍（如实说明）

* 只支持 npm lockfile **v3**。v1/v2、yarn、pnpm、shrinkwrap 显式拒绝。
* 范围解析覆盖文档化 npm semver 子集；遇到任何无法证明的 spec（VCS、URL、
  alias、dist-tag、overrides 等）一律报错，绝不“放行未知”。
* 制品必须在输入包内 `vendor/<host>/<path>/<file>.tgz`（按 `resolved` URL
  映射）。本服务不联网下载——离线门禁的前提就是一切证据自带。
* 平台判定基于请求时指定的 os/cpu/libc；可选包不匹配平台是 info，必选包不匹配
  是 error。
* 循环依赖在 npm 中可运行，故记为 warning 而非 error，但会给出最短环。
* 完整性只接受 sha512/sha384/sha256 为强算法；sha1 能解析并比对，但产生告警并
  使可复现性为 false。
