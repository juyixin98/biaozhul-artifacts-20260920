# reprobuild — 可复现归档打包服务（纯后端）

用 Go 实现的本地构建工程服务：把源树物化为**隔离的每构建工作目录**，**只运行请求中显式提供的测试夹具（fixture）命令**，然后把结果打成**确定性 tar 制品**。

- **纯本地**：默认只监听 `127.0.0.1`；不连接、不依赖任何云平台（代码中无任何对外出站客户端调用）。
- **缓存与工作目录分离**：`work/` 存放每次构建的临时树，`cache/` 存放内容寻址（SHA-256）的只读制品，`data/` 存放构建记录。
- **可复现**：内容相同的两棵树，即使目录遍历顺序不同、源文件 mtime 不同、主机 umask/uid/gid 不同，产出的 tar **逐字节一致**。
- **无前端**：仅 JSON HTTP 接口与两个 CLI 子命令。

## 确定性（reproducibility）保证

打包时做了如下归一化，见 `internal/archive/tar.go`：

| 维度 | 策略 |
|---|---|
| 条目顺序 | 按归档名严格排序（目录条目排在其内容之前，与 GNU tar 一致），与 readdir 顺序无关 |
| 修改时间 | 所有条目统一固定为 Unix epoch 0（UTC），可用 `-fixed-time` / `fixed_time_seconds` 覆盖 |
| 访问/改变时间 | atime、ctime 一律为零 |
| 属主 | uid/gid 固定为 0，uname/gname 留空 |
| 权限 | 目录 `0755`、普通文件 `0644`、符号链接 `0777`（可对每类设置 `PreserveMode` 保留源权限） |
| tar 格式 | 强制 PAX，长文件名（>100 字节）与 Unicode 路径可移植 |
| 根目录 | 顶层根目录本身不入包，但内部**空目录保留** |
| 文件内容 | 字节原样写入；内容不同则哈希必不同 |

最终制品写入 `cache/<sha256>.tar`，内容相同的两次构建共享同一个缓存文件（硬链接落盘，跨设备回退为复制）。

## 安全模型

- **符号链接逃逸拒绝**：打包前对每个链接做逐组件解析（不跟随目录、不依赖 `filepath.EvalSymlinks` 的隐式语义）。链接的最终解析位置（含断链的词法目标）一旦落在源根之外即拒绝；符号链接环也拒绝。根内断链（指向根内尚不存在的目标）允许保留。
- **fixture 产物同样受检**：夹具命令可以在工作目录里创建符号链接，打包前会再次校验，`ln -s ../../../../etc` 这类逃逸会让整个构建失败且不产生制品。
- **非可移植类型拒绝**：设备文件、FIFO、socket 不入包。
- **内联文件路径约束**：拒绝绝对路径与 `..` 穿越。
- **夹具执行隔离**：`exec.CommandContext` 直接执行（**不经 shell 拼接**，`sh -c` 仅在用户显式把 `sh` 作为命令时发生）；工作目录为隔离临时目录；环境变量采用白名单（默认仅 `PATH/LANG/LC_*/TZ/HOME/TMPDIR`，`TZ=UTC`），宿主机上的密钥不会泄露给夹具；每个夹具有超时，超时退出码 124。

## 目录结构

```
go.mod
cmd/
  reprobuild/            # 服务/打包主程序（serve、pack）
  acceptance/            # 端到端验收程序（16 项检查，输出 JSON 摘要）
internal/
  archive/               # 确定性 tar 打包 + 符号链接校验
  builder/               # 本地构建服务：物化源树→跑夹具→打包→入缓存
  api/                   # JSON HTTP 接口
examples/                # 请求样例与 curl 脚本
docs/run/                # 实际运行记录与验收摘要
```

## 构建与测试

```bash
go build ./...
go vet ./...
gofmt -l .               # 应无输出
go test ./...            # 单元/HTTP 测试
go test -race ./...      # 竞态检测
go build -o bin/reprobuild ./cmd/reprobuild
go build -o bin/acceptance ./cmd/acceptance
```

## 运行

### 1. HTTP 服务（本地）

```bash
./bin/reprobuild serve -addr 127.0.0.1:18391 -state-dir ./.reprobuild
```

### 2. 一次性打包 CLI

```bash
./bin/reprobuild pack -src ./some-tree -o artifact.tar
# 用乱序 readdir 再打一次，输出必须逐字节相同（自检用）：
./bin/reprobuild pack -src ./some-tree -o artifact2.tar -shuffle-seed 99
cmp artifact.tar artifact2.tar
```

### 3. 端到端验收程序

```bash
# 自动临时目录（可重复运行）
./bin/acceptance -json-out /tmp/acceptance.json
# 或保留工作产物以便检查
./bin/acceptance -json-out docs/run/acceptance-summary.json -work docs/run/acceptance-work
```

16 项检查覆盖：不同 mtime 双树字节一致、乱序遍历字节一致、Unicode（中文/拉丁变音符/emoji）路径、空目录、>100 字节长文件名（PAX）、mtime 固定、条目排序、tar 往返可读、相对/绝对/断链逃逸与链接环拒绝、HTTP 构建/缓存一致/逃逸拒绝/制品哈希一致。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/healthz` | 健康检查 |
| POST | `/api/v1/builds` | 创建并执行一次构建 |
| GET | `/api/v1/builds/{id}` | 查询构建记录 |
| GET | `/api/v1/builds/{id}/artifact` | 下载 tar（响应头带 `X-Artifact-Sha256`、`X-Artifact-Entries`） |

### POST /api/v1/builds 请求体

```json
{
  "source_dir": "/可选的本地源目录（先复制进隔离工作目录）",
  "files": [
    {"path": "src/数据.txt", "content": "你好\n", "mode": 420}
  ],
  "fixtures": [
    {"name": "render", "args": ["sh", "-c", "echo ok > out.txt"],
     "env": {"K": "V"}, "timeout_seconds": 30}
  ],
  "keep_work": false,
  "fixed_time_seconds": 0
}
```

- `source_dir` 与 `files` 至少给一个；`files` 会叠加在复制来的源树之上。
- **只有 `fixtures` 里的命令会被执行**，按顺序运行；任一非零退出即停止，记录 `status="fixture_failed"`，不产生制品。
- 打包/校验错误（含符号链接逃逸）记录 `status="error"`。
- 成功为 `status="ok"`，`artifact.sha256` 即内容地址。

### 响应示例（节选）

```json
{
  "id": "9d2a84053266754d",
  "status": "ok",
  "fixtures": [ {"name": "render", "exit_code": 0, "stdout": "", "duration_millis": 4} ],
  "artifact": {
    "sha256": "cf747397…1d3f",
    "size": 7168,
    "entries": 7,
    "path": "/…/cache/cf747397…1d3f.tar"
  },
  "created_at": "2026-09-23T19:07:43.594144072Z"
}
```

更多样例见 [`examples/`](examples/)，可直接运行 [`examples/curl-examples.sh`](examples/curl-examples.sh)。

## 设计取舍与边界

- 制品是 **tar（PAX）**，不含压缩：gzip/xz 头部的确定性需要额外处理，且压缩后字节复现性对压缩器版本敏感。需要压缩时建议对确定性 tar 再做 `gzip -n`。
- 夹具在宿主机直接执行受信命令（题目前提：仅运行用户显式提供的夹具），不做容器/虚拟化隔离；隔离的是**工作目录、环境变量和超时**，不是系统调用边界。
- 固定属主为 `0/0`，保证跨主机一致；在非 root 下解包时 tar 会保留头信息但不会真正 chown。
- 服务只提供 HTTP/JSON，未实现鉴权、多租户与任务队列——定位为本地构建工具。
- 构建记录以追加 JSONL（`data/records.jsonl`）持久化，重启可查询；制品不落对象存储，仅在本地缓存目录。
