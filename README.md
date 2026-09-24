# 构建缓存键审计 (buildcache)

本地编译任务的缓存后端:Go + HTTP + SQLite,纯后端,无前端。
缓存键绑定**源码清单、工具链摘要、构建命令、目标平台、声明环境变量**;
所有哈希、编译、运行均真实执行,失败如实报告,绝不缓存成功假象。

## 缓存键的组成

`key = SHA-256( 规范化序列化( 平台 | 命令 | 工具链 | 声明环境 | 源码清单 ) )`

- **源码清单**:声明的每个源文件按路径字节序排序,记录 `路径 + 状态 + 大小 + SHA-256`。
  不存在的文件记为 `missing`(无哈希),空文件记为 `present` 且哈希为空内容
  的 SHA-256(`e3b0c442…`)——两者产生**不同的键**。声明顺序、重复声明不影响键。
- **工具链摘要**:编译器二进制的 SHA-256 + 其 `--version` 真实输出的 SHA-256。
- **声明环境变量**:只有声明的变量进入子进程环境(PATH/TMPDIR 除外),
  未声明的环境无法泄漏进构建;遗漏声明会得到不同的键,而不是陈旧的命中。
- **平台**:`GOOS/GOARCH` + `uname -srm`。

规范化编码对每个字段做长度前缀,不存在拼接歧义。`GET /v1/audit/{key}`
可查看任意键的完整输入分解。

## 行为保证

- **单发布者**:同一键的并发请求中,只有一个客户端获得 SQLite 租约并真正
  执行构建,其余等待发布结果后共享命中;发布者崩溃时租约可超时接管。
- **失败不缓存成功**:构建失败、产物缺失、冒烟运行失败都记为 `failed`,
  永远不会作为命中返回;修复后同一键可重新构建。
- **命中校验**:每次命中前重新哈希产物字节并核对大小;损坏或丢失的产物
  被移入 `data/quarantine/` 隔离,条目标记 `quarantined`,随后自动重建。
- **重启恢复**:元数据在 SQLite、产物在 CAS(`data/cas/<sha256>`,临时文件
  fsync 后原子改名),重启后缓存继续有效;上次异常退出时处于 `building`
  状态的条目在启动时标记为 `failed`。
- **冒烟运行**:每次构建成功后以无参数真实运行产物并保存输出
  (`run_output`),作为缓存条目真实可用的证据。

## 目录结构

```
cmd/cacheserver/    HTTP 服务
cmd/cachectl/       CLI 客户端
internal/cachekey/  缓存键计算与源码清单
internal/toolchain/ 工具链解析(二进制哈希 + 版本探测)
internal/cas/       内容寻址产物存储 + 隔离
internal/store/     SQLite 元数据(租约、条目、重启恢复)
internal/builder/   真实执行构建与冒烟运行
internal/server/    HTTP 编排
examples/hello/     真实 gcc 编译示例(CPATH 选择头文件版本)
scripts/acceptance.sh  端到端验收脚本
```

## 启动

```sh
go build -o bin/cacheserver ./cmd/cacheserver
go build -o bin/cachectl    ./cmd/cachectl

./bin/cacheserver -addr 127.0.0.1:8080 -data data -workspace .
```

## 验收命令

```sh
# 自动化测试(键计算、单发布者、失败不缓存、隔离、重启、真实 gcc 编译)
go test ./...

# 依赖锁定校验
go mod verify

# 端到端验收:miss→hit→环境失效→损坏隔离→丢失重建→重启命中→并发单发布者
./scripts/acceptance.sh
```

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/builds` | 提交构建任务,返回 `cache: hit/miss`、键、产物摘要、冒烟输出 |
| GET  | `/v1/builds/{key}` | 查询单个缓存条目 |
| GET  | `/v1/audit/{key}` | 查看键绑定的全部输入(审计) |
| GET  | `/v1/entries` | 列出全部条目(含 failed/quarantined) |
| GET  | `/healthz` | 存活检查 |

任务格式(`examples/hello/task.v1.json`):

```json
{
  "task_dir": "examples/hello",
  "sources": ["main.c", "util.c", "util.h"],
  "command": "gcc -O2 -Wall -Werror -o \"$OUT\" main.c util.c",
  "env": {"CPATH": "include-v1"}
}
```

- `task_dir`:相对服务 `-workspace` 根的目录,构建在此目录内以 `sh -c` 执行。
- `sources`:声明的源文件,进入清单;不存在的文件返回 422 并单独列出。
- `command`:必须把产物写到 `$OUT`;`$SOURCES` 为 shell 引用后的源文件列表。
- `env`:声明的环境变量,进入缓存键并传入子进程。

## 手动示例

```sh
./bin/cachectl build examples/hello/task.v1.json   # miss,真实 gcc 编译
./bin/cachectl build examples/hello/task.v1.json   # hit,同一产物摘要
./bin/cachectl build examples/hello/task.v2.json   # CPATH 变化 → miss,输出 2.0.0
./bin/cachectl build examples/hello/task.noenv.json # 未声明 CPATH → 如实编译失败
./bin/cachectl audit <key>                          # 查看键绑定的全部输入
./bin/cachectl list                                 # 全部条目
```

示例工程中 `main.c` 包含的 `app_config.h` 由 `CPATH` 选择的
`include-v1/` 或 `include-v2/` 提供,因此声明环境直接决定产物内容,
可用于证明环境绑定与失效行为。

## 依赖

唯一直接依赖:`modernc.org/sqlite v1.29.10`(纯 Go SQLite,无 cgo),
版本锁定于 `go.mod` / `go.sum`,`go mod verify` 可校验。
