# 实际运行记录（RUNLOG）

- 日期：2026-09-24
- 环境：Linux 6.8.0-90-generic x86_64；Go 1.22.2；仅标准库，无第三方依赖
- 目录：`/home/admin/Downloads/biaozhul/opp105/b`
- 所有命令均在本机运行；服务只绑定 `127.0.0.1`，无任何出站/云连接。

## 1. 构建与静态检查

```text
$ go version
go version go1.22.2 linux/amd64

$ go build ./...
build OK

$ go vet ./...
vet OK

$ gofmt -l .
（最终为空：全部文件已 gofmt）
```

## 2. 单元/集成测试

```text
$ go test -count=1 ./...
?   deltaupdate/cmd/delta-update   [no test files]
ok  deltaupdate/internal/apply     0.215s
ok  deltaupdate/internal/delta     0.058s
ok  deltaupdate/internal/server    0.047s
ok  deltaupdate/internal/store     0.008s
ok  deltaupdate/test/crash         0.712s
```

竞态检测：

```text
$ go test -race -count=1 ./...
ok  deltaupdate/internal/apply     2.633s
ok  deltaupdate/internal/delta     1.470s
ok  deltaupdate/internal/server    1.167s
ok  deltaupdate/internal/store     1.023s
ok  deltaupdate/test/crash         1.944s
```

无数据竞争。

### 关键断言的实际结果

- 插入导致块位移：`TestRoundTripInsertionShiftsBlocks`
  对 40 个 512B 块在偏移 37 处插入 17 字节，结果
  `insertion: 39 copies, 1 data ops`——边界整体位移后仍复用 39/40 块。
- 随机 fuzz（普通测试）：`TestRoundTripRandomFuzz`，60 组随机
  插入/删除/替换，全部往返一致。
- 原生 fuzz：

  ```text
  $ go test ./internal/delta/ -run FuzzGenerateRoundTrip \
      -fuzz=FuzzGenerateRoundTrip -fuzztime=20s
  ... execs: 203640 ... PASS
  ```

  203,640 组随机 (old, new, blockSize) 全部往返一致，无崩溃。

## 3. 子进程硬崩溃验收（test/crash）

该套件先 `go build` 真实 CLI，再以子进程方式运行，通过环境变量在每个
阶段触发 `os.Exit(42)`，断言：崩溃后制品路径要么是旧摘要（rename 前），
要么是新摘要（rename 后），绝不为半截垃圾；随后无故障重跑必须收敛到
`newSum`，退出码 0。

```text
$ go test ./test/crash -count=1
ok  deltaupdate/test/crash   0.7s
```

覆盖阶段：`check-old, space, prepare, write-delta, sync, verify-delta,
rename, post-rename, fsync-dir, verify-new`（10 个）。另有
`TestCrashThenCrashAgain`（连续两次写一半崩溃，验证残留 temp 在锁内清理）、
`TestCLIWrongBase`(期望退出码 10)、`TestCLICorruptPatch`(11)、
`TestCLISpacePreflight`(12)、`TestCLIFullGenerateAndApply`（纯 CLI
generate+apply 端到端）。

## 4. 手工 CLI 逐阶段崩溃（真实命令输出摘录）

以约 72KB 制品、块 1024 生成补丁后逐阶段打崩，再重跑：

```text
stage=check-old     exit=42 digest=ac33f6ff49fb OLD-PRESERVED ; retry -> converged(rc=0)
stage=space         exit=42 digest=ac33f6ff49fb OLD-PRESERVED ; retry -> converged(rc=0)
stage=prepare       exit=42 digest=ac33f6ff49fb OLD-PRESERVED ; retry -> converged(rc=0)
stage=sync          exit=42 digest=ac33f6ff49fb OLD-PRESERVED ; retry -> converged(rc=0)
stage=verify-delta  exit=42 digest=ac33f6ff49fb OLD-PRESERVED ; retry -> converged(rc=0)
stage=rename        exit=42 digest=ac33f6ff49fb OLD-PRESERVED ; retry -> converged(rc=0)
stage=post-rename   exit=42 digest=723aad50662a NEW-VISIBLE   ; retry -> converged(rc=0)
stage=fsync-dir     exit=42 digest=723aad50662a NEW-VISIBLE   ; retry -> converged(rc=0)
stage=verify-new    exit=42 digest=723aad50662a NEW-VISIBLE   ; retry -> converged(rc=0)
stage=write-delta   exit=42 digest=ac33f6ff49fb (OLD-PRESERVED); retry -> converged
```

错基线 / 损坏补丁 / 空间不足：

```text
--- wrong base ---  exit=10   文件内容保持为原错误基线，未被改动
--- corrupt patch --- exit=11 old artifact unchanged: YES
--- no space ---    exit=12   old artifact unchanged: YES
--- happy path ---  exit=0
sha256: 299b900d...   == expected new: 299b900d...   （最终摘要核验一致）
```

## 5. SIGKILL（不可捕获）中途中断

构造 300MB 制品，启动 apply，**等待 `.delta.tmp` 出现确认正处于
write-delta 阶段**后 `kill -9`：

```text
SIGKILL 时临时文件大小=2719744（部分写入）
目标摘要=c452b942e681e7e5 旧摘要=c452b942e681e7e5
PASS: SIGKILL 中途，旧制品字节不变
残留文件: art.bin, art.bin.delta.lock, art.bin.delta.tmp
-- 重跑（flock 已由内核释放；残留 temp 在锁内被清除）--
{"applied":true,...,"newDigest":"bb3fafea..."}
PASS: 重跑收敛 newSum
```

说明：`SIGKILL` 后锁文件与临时文件留在磁盘上是预期行为；锁是内核持有的
`flock`，进程被杀即释放，不会死锁；下次应用在锁内删除残留 temp 再继续。

## 6. HTTP 端到端

```text
$ ./examples/requests.sh
== 1) 健康检查 {"status":"ok"}
== 2) 上传旧/新制品到缓存 -> 返回各自 sha256
== 3) 生成补丁：71 copy / 2 data（开头插入仍高复用）
== 4) 预置旧制品
== 5) 原子应用 {"applied":true,"oldDigest":"ef9d…","newDigest":"1191…"}
== 6) 核验最终摘要 PASS: 最终摘要 == newSum
== 7) 再次应用 applied=false（幂等）
== 8) 错基线 HTTP 409 wrong_base  PASS
== 9) 空间不足 HTTP 507 insufficient_space  PASS
全部演示通过。
```

## 7. 开发过程中实际遇到并修复的问题（如实记录）

1. **statfs 在测试中一度返回 0**：根因不是文件系统，而是 HTTP 请求体
   `simFreeBytes` 用了普通 `int64`，省略该字段时 JSON 零值 0 被误判为
   “已提供空间模拟”，导致每个请求都按 0 可用字节预检。改为 `*int64`
   区分“未提供/提供 0”后修复。（排查时用独立小程序确认
   `syscall.Statfs` 本身正常。）
2. **子进程测试二进制路径失效**：最初用某个测试的 `t.TempDir()` 存放
   编译出的 CLI，该测试结束目录被清理导致后续子进程
   `fork/exec ... no such file or directory`。改为在 `TestMain` 中构建到
   独立持久临时目录，套件结束统一删除。
3. **`rename` 阶段缺少硬崩溃注入点**：第一次跑崩溃套件时该阶段退出码为
   0（只接了可恢复故障，漏了 `crashAt(StageRename)`），补上后在
   rename(2) 之前崩溃，符合“rename 前旧制品必须保留”的语义。
4. **滚动匹配早期版本对离开窗口字节的归类有歧义**，重构为“离开窗口字节
   先进入字面缓冲、命中时由 acceptMatch 先冲刷字面再发 copy”，并由
   20 万次 fuzz 与随机往返测试确认无字节丢失/重复。
5. 补丁中 copy 块索引最初限制为“严格递增”（经典 rsync 串行读基线下的
   约束）。本实现重建时按偏移 `Seek` 旧制品，可任意顺序/重复引用，故
   放宽该限制，使“新制品重复旧内容”也能高复用（对应
   `TestRepeatedDataCompressesToCopies`）。
6. 示例脚本固定端口 18080 首次运行遇到端口占用，改为自动选取回环空闲
   端口（可用 python3 时），并支持 `ADDR` 覆盖。

## 8. 未通过项 / 已知限制

- 最终交付状态下：**无失败测试、无 vet/race/fuzz 失败**。上文所有
  “FAIL”均为开发中途的迭代现象，已修复并复测通过。
- 非 Linux 平台的锁退化为 `O_EXCL` 锁文件，`SIGKILL` 后不会自动释放
  （已在 `lock_other.go` 注释说明）；硬崩溃验收在 Linux 运行。
- 不做缓存垃圾回收、不做前端、不做多副本/分布式同步（不在本次需求内）。
