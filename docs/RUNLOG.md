# 运行记录（RUNLOG）

- 日期：2026-09-24
- 环境：Linux 6.8.0-90-generic x86_64，Go 1.22.2，`/bin/sh`（dash）
- 记录原则：只记录实际执行过的命令与真实输出；未通过项与修复过程如实保留。

## 1. 编译与静态检查

```
$ go version
go version go1.22.2 linux/amd64

$ go build ./...        # 通过
$ go vet ./...         # 通过（VET_OK）
$ go build -o bin/bis ./cmd/bis   # 通过，产出可执行文件
```

## 2. 自动化测试

```
$ go test ./... -count=1
ok  	bis/internal/api          0.097s
ok  	bis/internal/builder      0.116s
ok  	bis/internal/impact       0.083s
ok  	bis/internal/provenance   0.003s
ok  	bis/internal/repro        0.132s
ok  	bis/internal/safeio       0.004s
ok  	bis/internal/spec         0.003s
ok  	bis/internal/store        0.005s
ok  	bis/internal/verify       0.167s
# bis/cmd/bis、bis/internal/digest、bis/internal/testutil 无测试文件（?）

$ go test -race ./...    # 全部包通过，无数据竞争
```

**共 53 个测试用例全部 PASS，0 失败、0 跳过。** 完整输出保存于
`logs/test-output.log`。

验收相关用例与对应关系：

| 验收点 | 测试 | 结果 |
|---|---|---|
| 共享依赖（菱形）构建与传递来源 | `builder.TestBuildDiamondExecutesInOrder` | PASS |
| 增量缓存、源变更只失效受影响动作 | `TestBuildCachesOnSecondRun`、`TestSourceChangeInvalidatesCache` | PASS |
| 缓存与工作目录分离、blob 不可变 | `TestWorkDirDisposableAndSeparateFromCache`、`store.TestBlobContentAddressingAndDedup` | PASS |
| 只执行声明的夹具命令 | `TestExecutedCommandsAreOnlyDeclaredFixtures`、报告字段 `commands_run` | PASS |
| 独立重算摘要核验 | `verify.TestVerifyIndependentlyRecomputesDigests` 等全部 verify 用例 | PASS |
| 未重签篡改（记录文件被改） | `TestUnsignedRecordTamperDetected`（SIGNATURE_INVALID + RECORD_HASH_MISMATCH） | PASS |
| **持密钥重签的伪造链接** | `TestReSignedForgedLinkCaughtByCrossRecordBinding`（UPSTREAM_DIGEST_MISMATCH） | PASS |
| 改链接但不修指纹 | `TestForgedLinkWithoutFingerprintFixCaughtTwice` | PASS |
| **循环伪造** | `TestForgedCycleDetected`（base↔child 回边，环上节点 CYCLE_DETECTED） | PASS |
| 源篡改传递污染 | `TestSourceTamperBreaksChainTransitively`（link: UPSTREAM_CHAIN_BROKEN） | PASS |
| 工具篡改 | `TestToolTamperDetected`（三个动作全部 TOOL_DIGEST_MISMATCH） | PASS |
| 缺失 blob / 缺失记录 | `TestMissingBlobDetected`、`TestMissingIndexEntryDetected` | PASS |
| 影响面：共享源全命中、分支源不串支 | `impact.TestImpactSharedSourceBlastRadius`、`TestImpactBranchSourceScopedToOneBranch`、`TestImpactByDigest` | PASS |
| 隔离冷缓存重算 | `repro.TestReproUsesIsolatedStore`、`TestDeterministicBuildReproduces` | PASS |
| **完整溯源但不可复现** | `TestCompleteProvenanceButNotReproducible`（nondet 夹具） | PASS |
| **溯源不完整但仍可复现** | `TestIncompleteProvenanceButStillReproducible`（删 blob 后重算一致） | PASS |
| 路径逃逸 / 符号链接逃逸 | `spec` 校验用例、`safeio.TestResolveWithinSymlinkEscape` | PASS |
| HTTP 全生命周期 / 非法导入 | `api.TestFullLifecycleOverHTTP`、`TestImportRejectsInvalidAndSymlinks`、`TestReproduceEndpoint` | PASS |

## 3. 实机 HTTP 演练（真实服务进程）

命令：

```
$ ./bin/bis -addr 127.0.0.1:<临时端口> -root /tmp/bis-live -timeout 15s
$ BASE=http://127.0.0.1:<端口> OUT=logs/live ./scripts/live-demo.sh
```

（说明：先尝试的 18080/18091 端口已被本机其他进程 vccsim/ws 占用，服务如实
报 `bind: address already in use`，随后改用内核分配的空闲端口成功。）

11 步响应逐个保存在 `logs/live/01..12-*.json`，关键结果汇总：

| 步骤 | 结果 |
|---|---|
| 1 导入 shared-demo | 201，3 个动作 |
| 2 首次构建 | `status=ok`；compile_a/compile_b/link 全部 **executed**，`commands_run` 3 条 |
| 3 再次构建 | 三个动作全部 **cached**，`commands_run` 为空（无命令被派生） |
| 4 验证 | **complete=true**，findings=0（HTTP 200） |
| 5 影响面 `src/shared.txt` | affected = compile_a, compile_b, **link**；含两条汇入 link 的链 |
| 6 影响面 `src/a.txt` | affected = compile_a, link；**compile_b 不在其中** |
| 7 link 溯源记录 | 2 条上游摘要绑定，HMAC 签名长度 64 hex |
| 8 隔离重算 | **reproduced=true**，重跑真实执行 3 条命令（冷缓存） |
| 9-10 nondet 导入+构建 | 201 / ok |
| 11 nondet 隔离重算 | HTTP 409，**reproduced=false**；原摘要 `69950ceb…` ≠ 重算 `2845934b…` |
| 12 图视图 | 节点 compile_a/compile_b/link |

### 3.1 实机篡改检测

```
$ echo "alpha TAMPERED in imported store" > /tmp/bis-live/projects/shared-demo/src/a.txt
$ curl -i .../verify
HTTP/1.1 409 Conflict
```
响应（`logs/live/13-verify-tampered.json`）：

- `compile_a`：`SOURCE_DIGEST_MISMATCH`（磁盘 6a5eff76… ≠ 记录 1594c312…）
  与 `FINGERPRINT_MISMATCH`；
- `compile_b`：仍 `complete=true`（它不读 a.txt）；
- `link`：`complete=false, tainted=true, UPSTREAM_CHAIN_BROKEN`。

实机流程的服务器日志：`logs/live-server.log`；演练完整标准输出：
`logs/live-walkthrough.log`。

## 4. 过程中出现并已修复的问题（如实记录）

1. 夹具初版未 `mkdir -p` 输出目录 → `cannot create out/a.bundle: Directory
   nonexistent`。修复：夹具脚本先建输出目录。
2. 源最初物化到 `work/src/src/...`（双前缀）导致 `cat: src/shared.txt: No
   such file or directory`。修复：声明源路径相对工作目录根复制；`in/` 保留
   给上游制品并在 spec 中禁用该前缀。
3. 上游物化目录未预建 → `lstat .../in: no such file or directory`。修复：
   存在上游时先创建 `in/`。
4. 删除某动作的索引记录后，其子节点一度仍被判完整（记录图里丢了这条边）。
   修复：传递完整性的依赖图同时并入 **spec 声明边**，并新增
   `MISSING_UPSTREAM_RECORD` 码。
5. 多个本地名指向同一上游时指纹重复计数。修复：按 `(上游动作, 输出)` 去重。

以上修复均有对应测试锁定；修复后 53 个用例与 `-race` 全部通过。

## 5. 未通过项 / 已知限制

- 当前最终状态：**无未通过测试**（go test 与 go test -race 全绿，go vet 通过）。
- 已知设计限制（非缺陷）：
  - HMAC 不防御持有服务密钥的内部人；对持密钥伪造来源的防御依赖跨记录摘要
    绑定与指纹重算（README 第 6 节、verify 测试已实证）。
  - 夹具在进程级隔离（私有 CWD/TMPDIR、白名单环境、超时）中运行，未使用
    容器/虚拟机级强隔离；服务仅监听回环地址且只执行显式声明工具。
  - 源/输出仅支持常规文件（不支持目录产物、符号链接）。
