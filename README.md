# 可复现归档打包服务（Reproducible Archive Builder）

一个纯后端、仅本地运行的确定性 tar 制品打包服务。给定一棵源目录树，
生成**字节级可复现**的 tar 归档：相同内容无论目录遍历顺序、文件 mtime、
umask、构建机器/时间如何不同，都产出同一个 SHA-256 的制品。

- **纯后端**：Go 标准库实现的 JSON HTTP 接口，无前端、无第三方依赖。
- **仅本地**：只处理调用方显式指定的本地目录，**不连接任何云平台**，不执行任何外部命令。
- **缓存 / 工作目录分离**：暂存与清单在工作目录，内容寻址制品在独立缓存目录。
- **安全**：拒绝任何会逃逸出归档根的符号链接；拒绝设备/FIFO 等非常规文件。

---

## 1. 确定性策略

制品字节只取决于「归档内相对路径集合 + 文件内容 + 显式请求选项」：

| 维度 | 策略 |
|---|---|
| 条目顺序 | 全部收集后，按归档内相对路径的 **UTF-8 字节序**统一排序，与 `readdir` 返回顺序无关 |
| 修改时间 | 统一写固定时间，默认 `1970-01-01T00:00:00Z`（请求可用 `fixed_modtime_unix` 指定） |
| 权限 | 普通文件 `0644`、目录 `0755`、符号链接 `0777`；`preserve_exec=true` 时磁盘上带任意执行位的文件归一化为 `0755` |
| 属主 | uid/gid 一律 `0`，uname/gname 为空 |
| 时间扩展字段 | 不写 atime/ctime；epoch mtime 不产生多余 PAX 时间记录 |
| 格式 | PAX，长文件名 / Unicode 由标准库确定性生成（PAX 记录键已排序） |
| 符号链接 | 只存链接本身，不跟随；词法解析与磁盘真实解析都必须位于归档根内，否则整个构建被拒绝 |
| 非常规文件 | 设备、套接字、FIFO 等直接报错，不静默丢弃 |

空树产出仅含两个 512 字节零块的最小 tar（1024 字节）。

## 2. 目录结构

```
cmd/server/             HTTP 服务入口
internal/archive/       确定性 tar 打包核心（internal 包，含完整单测）
internal/service/      构建编排：暂存、内容寻址缓存、清单持久化
internal/httpapi/      JSON 接口与错误分类
examples/              请求样例
scripts/acceptance.sh  端到端验收脚本（自动造夹具、启动服务、比对）
reports/               验收运行日志
```

运行期目录（默认在 `.local/` 下，已 gitignore）：

```
<workDir>/manifests/<build-id>.json   每次构建清单（重启后可恢复查询）
<workDir>/tmp/                        构建期暂存（成功后清空）
<cacheDir>/artifacts/<sha256>.tar     内容寻址、不可变的缓存对象
```

## 3. 快速开始

需要 Go 1.22+。

```bash
# 编译并启动（默认 127.0.0.1:8080）
go run ./cmd/server -addr 127.0.0.1:8080 -work ./.local/work -cache ./.local/cache

# 发起构建（路径必须为绝对路径）
curl -sS -X POST http://127.0.0.1:8080/v1/builds \
  -H 'Content-Type: application/json' \
  -d @examples/build-request.json
```

## 4. JSON 接口

所有请求/响应均为 `application/json; charset=utf-8`。

### `POST /v1/builds`

请求：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `source_dir` | string | 是 | 待打包本地目录，**绝对路径** |
| `output_path` | string | 是 | 制品输出位置，**绝对路径**，原子替换；不允许位于缓存目录内 |
| `preserve_exec` | bool | 否 | 是否把可执行文件归一化为 `0755`（默认 `false`，全部 `0644`） |
| `fixed_modtime_unix` | int | 否 | 固定 mtime（Unix 秒），默认 `0`（epoch） |

成功 `201`，返回构建清单：

```json
{
  "build_id": "20260923T184743Z-8f1c…",
  "status": "succeeded",
  "source_dir": "/…/fixtures/tree1",
  "output_path": "/…/artifacts/tree1.tar",
  "preserve_exec": false,
  "fixed_modtime_unix": 0,
  "cache_hit": false,
  "artifact_sha256": "e4ad7489…",
  "artifact_size": 19968,
  "file_count": 6,
  "dir_count": 4,
  "symlink_count": 3,
  "entries": [
    {"path": "a.txt", "type": "file", "mode": 420, "size": 6,
     "sha256": "b6a98d9c…"}
  ],
  "created_at": "2026-09-23T18:47:43.123456789Z"
}
```

`entries` 中每个普通文件含内容 SHA-256，可用于脱离整包做逐文件比对。

错误响应统一为：

```json
{ "error": { "code": "unsafe_symlink", "message": "拒绝逃逸符号链接: …" } }
```

| 状态码 | code | 触发场景 |
|---|---|---|
| 400 | `invalid_request` | JSON 非法、路径非绝对、源不存在/不是目录、输出落在缓存目录内 |
| 404 | `not_found` | 查询的 `build_id` 不存在 |
| 422 | `unsafe_symlink` | 符号链接逃逸归档根（绝对目标、`../` 越界、经目录链接解析后跳出） |

### 其他端点

- `GET /healthz` → `{"status":"ok"}`
- `GET /v1/builds/{id}` → 单次构建清单
- `GET /v1/builds` → `{"builds":[…]}`，按创建时间倒序

## 5. 自动化测试

```bash
go vet ./...
go test ./...               # 全部单元/接口测试
go test -race ./...         # 带竞态检测
go test ./internal/archive/ -v
```

核心测试（`internal/archive/tar_test.go`）：

- **遍历顺序无关**：通过测试接缝把每层目录的 `readdir` 顺序整体反转，两次 tar 字节必须完全一致。
- **不同 mtime 同字节**：两棵基准 mtime 相差约 22 年的等价目录树，字节与 SHA-256 必须一致。
- 固定头字段（epoch mtime、`0644/0755/0777`、uid/gid 清零、条目排序、自定义 mtime）。
- 逃逸符号链接四类（绝对路径 / 直接 `../` / 子目录嵌套 `../` / 经目录符号链接间接跳出）必须被拒；
  树内相对链接、子目录内 `../sibling`、两级链接、悬空链接必须被保留。
- Unicode 路径、空目录、超过 100 字节的长路径（PAX）、空树、FIFO 拒绝、源不是目录等。

`internal/service` 覆盖缓存命中、工作/缓存物理分离、清单持久化与重启恢复；
`internal/httpapi` 用 `httptest` 覆盖全部端点与 201/400/404/422 状态码。

## 6. 一键端到端验收

```bash
scripts/acceptance.sh
```

脚本会：编译并跑全部 Go 测试 → 临时端口启动服务 → 构造两份 mtime 分别为
`2001-09-09` 与 `2023-11-14` 的目录树（覆盖 Unicode 路径、空目录、长文件名、
合法/悬空符号链接、磁盘可执行文件）→ 经 JSON 接口分别打包 → 断言：

- 两份 tar `cmp` 逐字节一致、SHA-256 相同，第二次 `cache_hit=true`；
- 剔除易变字段后的两份摘要 `diff` 为空；
- 用系统 GNU tar 独立验证排序、epoch mtime、权限、`0/0` 属主、
  Unicode/空目录/长路径条目、解包往返一致；
- 逃逸符号链接返回 422 且无制品落盘；400/404/列表行为；
- 工作目录无 tar、缓存目录无清单、相同内容只产生 1 个缓存对象。

完整日志写入 `reports/acceptance-<UTC时间戳>.log`。

## 7. 最近一次实际验收结果

见 `reports/` 目录。最近一次运行：**39 项检查全部通过（PASS: 39, FAIL: 0）**，
制品 `artifact_size=19968` 字节，SHA-256 为 `e4ad7489…7a185`
（该值随夹具内容而定，脚本每次都会重新实测）。

开发过程中验收脚本曾出现 4 处**断言脚本自身**的问题（服务行为本身始终正确）：
健康探针按紧凑 JSON 比较、GNU tar 对 `:00` 秒省略显示、权限串排序顺序、
UTF-8 locale 下 `awk length` 按字符计数。均已在脚本中修正（`jq -c`、
`1970-01-01 00:00`、C locale 排序、`LC_ALL=C awk`）并复跑通过，未对服务代码返工。

## 8. 明确不做的事

- 不连接云平台、不上传/下载任何制品；
- 不执行任何外部构建命令（没有 shell 执行面）；
- 不打包归档根之外的内容，不跟随逃逸符号链接；
- 不提供前端页面。
