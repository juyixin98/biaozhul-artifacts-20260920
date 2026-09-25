# 制品差量更新服务（deltaupd）

纯后端、纯本地的制品差量更新工程服务：本地构建工程 → 制品入库 → 块级差量补丁生成 → 原子应用。
不连接任何云平台；缓存目录、状态目录、工作目录三者分离；只运行调用方显式提供的构建命令。

## 特性

- **块级差量**：rsync 风格算法——固定块（默认 64 KiB）弱校验（类 Adler-32，可滚动）+ 强校验（SHA-256）。
  插入/删除导致的块位移由滚动窗口天然覆盖（见验收测试 `TestInsertionShift`）。
- **补丁绑定旧新摘要**：补丁头携带 `old_digest` / `new_digest` 及两者大小；应用前预检基线，
  应用后核验最终摘要，任一不符即拒绝。
- **原子应用**：新制品在工作目录暂存 → fsync → 摘要核验 → 内容寻址入库（临时文件 + rename）→
  原子切换“当前制品”引用（rename）。任一阶段失败或进程崩溃，旧制品保持可用；重试幂等。
- **空间预检**：入库与应用前按 statfs 可用空间检查（支持 `-space-cap` 模拟小磁盘），不足即拒绝，
  已有制品不受影响。
- **不修改正在读取的文件**：所有 CAS 对象写入后 chmod 0444 且只读打开（`O_RDONLY`）；
  旧制品在应用全程只读。
- **崩溃恢复**：服务启动时清理工作目录中的中断暂存；CAS 对象与引用不受崩溃影响。

## 目录结构

```
cmd/deltaupd/        服务主程序（含崩溃注入测试辅助模式）
internal/patch/      块级差量算法与补丁格式（Patch v1）
internal/storage/    内容寻址缓存（blobs/patches）、状态（refs/projects）、工作目录
internal/service/    编排：构建、入库、差量生成、原子应用（含故障注入点）
internal/api/        HTTP JSON 接口
internal/digest/     SHA-256 工具
examples/            请求样例与端到端演示脚本
docs/test-run.log    测试运行实录
```

运行时数据目录（三者分离）：

```
<cache>/   blobs/ patches/ tmp/     # 内容寻址缓存，可整体清空重建
<state>/   projects/ refs/          # 持久状态：项目配方、当前制品引用
<work>/                             # 临时工作目录（构建、应用暂存），启动时清理
```

## 构建与运行

```bash
go build -o deltaupd ./cmd/deltaupd
./deltaupd -addr 127.0.0.1:18080 -cache ./data/cache -state ./data/state -work ./data/work
# 可选: -space-cap <字节> 模拟磁盘可用空间上限（测试空间不足）
```

## HTTP JSON 接口

统一响应：`{"ok":true,"data":...}` 或 `{"ok":false,"error":{"code","msg"}}`。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/projects` | 注册项目配方 `{name, argv, artifact_path}`（构建命令显式提供，不经 shell） |
| GET  | `/v1/projects` / `/v1/projects/{name}` | 列出 / 查询项目 |
| POST | `/v1/projects/{name}/build` | 在工作目录运行构建命令，产物入库并设为当前制品 |
| POST | `/v1/artifacts/{name}` | 直接上传原始制品（请求体为字节流），设为当前制品 |
| GET  | `/v1/artifacts/{name}/current` | 查询当前制品摘要 |
| GET  | `/v1/artifacts/{name}/download?digest=` | 下载制品（默认当前） |
| POST | `/v1/patches` | 生成差量补丁 `{name, old_digest?, new_digest?}` |
| POST | `/v1/patches/ingest` | 下发补丁到本机缓存（请求体为补丁字节流） |
| GET  | `/v1/patches/{digest}` | 查看补丁头 |
| GET  | `/v1/patches/{digest}/download` | 下载补丁 |
| POST | `/v1/apply` | 原子应用补丁 `{name, patch_digest}` |

错误码：`not_found`(404)、`bad_request`(400)、`digest_mismatch`/`block_mismatch`/`corrupt_patch`(422)、
`no_space`(507)、`build_failed`/`internal`(500)。

请求样例见 `examples/requests/*.json`；完整可运行演示（构建端 + 设备端双服务，含错基线、
损坏补丁、最终摘要核验）：

```bash
bash examples/demo.sh        # 默认端口基号 19000
```

## 补丁格式（Patch v1）

```
魔数 "DUPDIFF1" | 头长度 uvarint | 头 JSON | 操作记录序列
头 JSON: {version, block_size, old_digest, new_digest, old_size, new_size, created_unix}
操作记录: tag(1B) | 长度 uvarint | 数据
  tag=1 COPY:    块索引 uvarint | 块长 uvarint | 该块 SHA-256(32B)
  tag=2 LITERAL: 新数据字节
```

应用时逐条流式执行：COPY 从旧制品只读读取并用记录中的强校验核验（错基线/损坏在此暴露），
LITERAL 直接写出；总输出必须严格等于 `new_size`，最终 SHA-256 必须等于 `new_digest`。

## 原子应用的阶段与崩溃安全

```
preflight   基线摘要比对 + 空间预检
stage-open  在工作目录创建暂存文件
stage-copy  流式重建新制品（同时累计 SHA-256）
verify      最终摘要必须等于 new_digest
commit      新制品 CAS 入库（幂等）→ rename 原子切换当前引用
```

任一阶段失败/崩溃：当前引用仍指向旧制品，旧制品完整可用；暂存残留由下次启动清理；重试幂等。

## 测试

```bash
go vet ./...
go test ./... -count=1          # 完整套件
go test ./... -count=1 -race    # 竞态检测
```

覆盖的验收场景（测试名 → 位置）：

| 验收项 | 测试 |
|---|---|
| 错基线（摘要不符） | `TestWrongBaselineDigest`、`TestHTTPWrongBaselineAndCorruptPatch` |
| 错基线（块强校验） | `patch.TestWrongBaseline` |
| 损坏补丁（头/体/截断/魔数） | `patch.TestCorruptPatch`、`TestTruncatedPatch`、`TestBadMagic`、`TestCorruptPatchHeaderAndBody` |
| 插入导致块位移 | `patch.TestInsertionShift`、`TestDeletionShift` |
| 应用各阶段崩溃（进程内故障注入） | `TestFaultInjectionAllStages`（5 个阶段） |
| 应用各阶段崩溃（真实子进程崩溃 + 恢复重试） | `cmd/deltaupd.TestCrashRecoveryAllStages` |
| 空间不足 | `TestNoSpace`、`TestHTTPNoSpace`、`storage.TestCheckSpace` |
| 最终摘要核验 | 所有 Apply 路径 + `TestEndToEndHTTP`（下载内容逐字节比对） |
| 只读对象不可写 | `storage.TestCASRoundTripAndDedup` |

## 运行实录（如实记录）

环境：go1.22.2 linux/amd64。以下为实际执行结果，完整输出见 `docs/test-run.log`。

- `go vet ./...`：通过，无输出。
- `go test ./... -count=1`：**全部通过，无失败项**（28 个顶层测试，38 个含子测试条目）。
  - `cmd/deltaupd` ok（≈12.7s）：5 个阶段真实子进程崩溃恢复 + 正常路径。
  - `internal/api` ok（≈4.5s）：端到端双服务、错基线 422、损坏补丁 422、空间不足 507。
  - `internal/patch` ok（≈4.2s）：差量算法、插入/删除位移、损坏/截断/错基线。
  - `internal/service` ok（≈11.5s）：构建夹具、故障注入 5 阶段、幂等重试。
  - `internal/storage`、`internal/digest` ok。
- `go test ./... -count=1 -race`：全部通过（较慢，≈200s，因子进程崩溃测试逐阶段构建运行）。
- `bash examples/demo.sh`：端到端演示通过——v1(204800B)→v2(205888B) 差量补丁 75164B，
  设备端应用后 `sha256sum` 与 `cmp` 逐字节核验一致；错基线/损坏补丁均被 422 拒绝且旧制品保持可用。
- 手工实测（curl + crash-helper）：`preflight/stage-open/stage-copy/verify/commit` 五阶段
  注入真实进程崩溃（exit=2），崩溃后当前引用均保持旧制品，重试全部成功，工作目录无残留。

**未通过项：无。**

## 取舍与限制

- 差量生成时将新制品整体读入内存（实现简单、仅标准库）；超大制品（GB 级）需改为流式窗口实现。
- 块大小固定 64 KiB；弱校验碰撞由强校验兜底。
- 构建命令以 `exec.Command(argv...)` 直接执行（不经 shell），调用方需自行保证命令可信；
  服务不沙箱化构建过程。
- 单制品为单文件字节流；目录树制品需先打包（如 tar）再入库。
