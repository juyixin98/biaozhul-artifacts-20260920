# 运行记录（RUNLOG）

本文件如实记录项目在开发机上的实际构建、测试与手工验证命令及结果。
所有命令均在仓库根目录执行；未通过的中间项与修复过程也一并保留。

## 环境

- Go：`go version go1.22.2 linux/amd64`
- OS：Linux 6.8.0-90-generic x86_64（Ubuntu）
- 起点：空目录（仅有 git 初始化）
- 无第三方依赖（`go.mod` 仅含 module 声明）。

## 自动化测试

命令：

```bash
go test ./...            # 常规
go test -race -count=1 ./...
go vet ./...
gofmt -l .
```

最终结果（2026-09-24）：

- `go build ./...`：通过。
- `go vet ./...`：无告警。
- `gofmt -l .`：无输出（全部已格式化）。
- `go test -race -count=1 ./...`：4 个包全部 `ok`，无数据竞争。
- 顶层测试用例：**46 通过 / 0 失败**。

覆盖点与对应用例：

| 验收点 | 测试 |
|---|---|
| 引号/尖括号基本解析、多余空白 | `scanner.TestExtract_BasicQuotedAndAngled` |
| 注释（行/跨行块）、字符串、字符常量内伪 include | `TestExtract_PseudoIncludesInCommentsAndStrings`、`TestExtract_EscapedQuotesInString` |
| 反斜杠续行（合法 include、注释吞掉下一行） | `TestExtract_BackslashContinuation` |
| 宏生成 include 明确报错 | `TestExtract_MacroGeneratedIncludeIsError`、`TestBuild_MacroIncludeFailsStatus`、`TestHTTP_MacroIncludeStatusOKButGraphError` |
| 畸形 include（未闭合/空/尾随 token） | `TestExtract_MalformedIncludes` |
| CRLF / UTF-8 BOM | `TestExtract_CRLFAndBOM` |
| 同名头区分 | `resolver.TestResolve_QuotedPrefersIncludingDir`、`builder.TestBuild_SameNameHeadersDistinguished`、`...UnderRootIsInternal` |
| 嵌套路径 | `TestResolve_NestedPath` |
| 尖括号跳过相对/引号目录 | `TestResolve_AngledSkipsRelativeAndQuoteDirs` |
| 循环引用 / 自环 / 不死循环 | `TestBuild_CycleDetected`、`TestBuild_SelfCycle` |
| 菱形包含边去重 | `TestBuild_DiamondIncludesDedupEdges` |
| 缺失 include → missing 节点 + warning | `TestBuild_CommentsPseudoAndMissing`、`TestResolve_Missing` |
| 文件删除 | `TestBuild_DeletedFileBecomesMissing`、`service.TestAffected_DeleteHeader` |
| 修改（共享头/独占头）受影响目标 | `TestAffected_ModifySharedHeader`、`TestAffected_ModifyExclusiveHeader` |
| 新增文件 | `TestAffected_AddHeader` |
| 先改后删、基线不变仍可追溯 | `TestAffected_ModifyThenDeleteAgainstSameBaseline` |
| 配置变化 / 无基线 → 全部受影响 | `TestAffected_ConfigChangeAffectsAll`、`TestAffected_NoBaselineTreatsAllAsAffected` |
| 缓存与工作目录分离 | `TestCacheDirMustBeOutsideRoot` |
| 增量缓存复用（进程内快路径/跨进程 hash） | `TestBuild_CacheReuseOnUnchanged`、`TestIncrementalReadReusesCache`、`TestIncrementalInProcessFastCache` |
| 输出稳定排序 | `TestBuild_StableOutputAcrossRuns` |
| HTTP 200/400/healthz | `TestHTTP_ScanAndAffected`、`TestHTTP_BadRequestAndHealth` |
| 缺失节点键不重复拼接路径 | `TestBuild_MissingNestedIncludeKeyNotDoubled`、`resolver.TestFirstCandidate` |

## 手工端到端验证

### CLI 演示

```bash
bash examples/demo.sh
```

实际关键输出：

- 首次扫描 `app/main.c` + `edit/main_edit.c`：`status=ok`，9 节点 8 边 0 环。
- 修改 `edit/hdr/version.h`：
  `changed.modified=['edit/hdr/version.h']`，`affected_targets=['edit/main_edit.c']`。
- 删除 `edit/hdr/version.h`：
  `changed.deleted=['edit/hdr/version.h']`，`affected_targets=['edit/main_edit.c']`，
  并产生 warning `unresolved quoted include "hdr/version.h"`，`status` 仍为 `ok`（缺失不致命）。
- 扫描 `bad/macro_include.c`：`status=error`，诊断
  `macro-generated/computed #include is not supported: #include EXTRA_HEADER`，
  CLI 退出码 `1`（已单独验证 `echo $? = 1`；干净扫描退出码 `0`）。
- 扫描 `cycle/main_cycle.c`：`cycles=[['cycle/a.h','cycle/b.h','cycle/a.h']]`。

### HTTP 演示

```bash
bash examples/curl-demo.sh
```

- `GET /healthz` → 200 `{"status":"ok"}`。
- 首次扫描 → `status ok, nodes 9, edges 8, cycles None`。
- 删除被依赖头后 `POST /api/v1/affected` → 正确列出 deleted 与受影响目标。
- `POST /api/v1/parse` → 注释/字符串内伪 include 全部被忽略，仅返回真实 include。
- 注：脚本初次使用 18080 端口与机器上无关进程 `vccsim` 冲突，已改为 18765
  并增加就绪等待；非程序缺陷。

### 多目标增量手工流程（临时目录）

依次：无改动 / 改独占头 `only_a.h` / 改共享头 `base.h` / 删除 `only_a.h`
（基线仅在开头建立一次）：

- 无改动：changed 全空，affected_targets `[]`。
- 改独占头：仅 `src/a.c` 受影响。
- 改共享头：`src/a.c`、`src/b.c` 均受影响。
- 删除：deleted 正确、产生 missing 节点、`src/a.c` 受影响；
  项目目录内未生成任何缓存文件（缓存全部落在独立 cache_dir）。

## 开发过程中出现并已修复的问题（如实记录）

1. **粗粒度 mtime 导致漏报修改**：开发机文件系统在同一秒内快速重写返回
   **相同 mtime 与 size**，初版仅凭 `mtime+size` 命中缓存，导致
   `TestAffected_ModifySharedHeader` 失败（modified 为空）。
   修复：缓存判定改为两级——本进程 stat 的可信 mtime 走快路径；跨进程从快照
   恢复（单调时钟丢失）时再读内容算 SHA-256 兜底。新增统计字段
   `hashes_checked`，并补 `TestIncrementalReadReusesCache` 等回归测试。
2. **循环检测失效**：DFS 中“已完成集合”判断先于“在途栈”判断，回边永远
   走不到。已调整判断顺序，循环/自环用例通过。
3. **缺失引号 include 的节点键一度重复拼接路径段**（如 `src/h/h/base.h`）：
   根因是缺失候选用了逻辑路径再 join 包含者目录。改为统一由
   `resolver.FirstCandidate` 按真实搜索顺序的首个候选生成稳定键。
   （注：`#include "h/base.h"` 出现在 `src/h/` 内时，首个候选本就是
   `src/h/h/base.h`，这是夹具写法问题，验证时已改为同目录写 `"base.h"`。）
4. **JSON 空集合输出为 `null`**：统一初始化为空切片/数组，输出稳定为 `[]`。
5. 若干编译期问题（未使用变量/导入、Go 不允许给命名 map 类型在测试包挂方法等）
   均已修复。

## 未通过项 / 已知边界

- 截至最后一次运行，自动化测试与两个演示脚本**全部通过，无未通过项**。
- 功能边界（非缺陷，见 README“已知边界”）：不做宏展开与条件编译分支选择，
  所有可达分支中的 include 都计入；include 路径内不识别注释（符合 C 标准）；
  不扫描目标可达集之外的文件。
