# RUNLOG — 实际运行记录（如实登记）

- 日期：2026-09-24（UTC）
- 环境：Linux 6.8.0-90-generic x86_64，Go `go1.22.2 linux/amd64`
- 原则：只运行 `testdata/fixtures/commands.json` 中**显式列出**的本地命令，
  以及为验证交付物（curl 样例脚本）所必需的本机回环请求；全程不联网、不接云平台。

## 1. 最终验收结果

`go run ./scripts/fixturetest`（机器生成的完整结果见 `reports/fixture-report.json`）：

- 夹具命令：**10/10 通过**（退出码逐一比对）
- HTTP 冒烟（真实子进程 + 内核临时端口）：**3/3 通过**
- `go test -count=1 ./...`：全部包 `ok`（expression / policy / judge / cache / server / tests）
- `go test -race -count=1 ./internal/...`：全部 `ok`，无数据竞争
- `go vet ./...`：无告警；`gofmt -l .`：无输出
- 夹具规模：判定 22 条、解析错误 15 条、HTTP 10 条、命令白名单 10 条

CLI 退出码实测：`allow=0`、`deny=1`、`unknown=3`、解析/用法错误 `=2`。

curl 交付样例（`examples/curl_examples.sh`，临时端口真实服务）实测：
13 个 `200`（healthz + policies + 11 次判定）+ 1 个 `400 parse_error`。

## 2. 实际执行过的命令与结果

| 命令 | 结果 |
|---|---|
| `go build ./...` / `go build -o bin/licensejudge ./cmd/licensejudge` | 通过 |
| `go vet ./...` | 通过（无输出） |
| `gofmt -l .` | 初次有格式差异 → `gofmt -w .` 后干净 |
| `go test -count=1 ./...` | 全部通过（见上） |
| `go test -race -count=1 ./internal/...` | 全部通过 |
| `go run ./scripts/fixturetest` | 首轮**有失败**，修复后 10/10 + 3/3 全过 |
| `bin/licensejudge eval ...`（5 种表达式） | 裁决与退出码均符合预期 |
| `examples/curl_examples.sh`（真实服务） | 首轮因**脚本自身 bug** 全 404，修复后全过 |
| 目录分离 / 非法策略负向验证 | 均按预期拒绝启动/加载，退出码 2 |

## 3. 发现的问题与处置（含未通过项，不粉饰）

### 3.1 首轮单元测试：2 个语义缺陷（已修复，有回归测试）

1. **OR 备选分支未展平**：`A OR B OR C` 左结合成 `(A OR B) OR C`，
   顶层只暴露出 2 个 alternatives，且 `selection` 错误地返回复合串
   `"MIT OR Apache-2.0"` 而非原子分支 `"MIT"`。
   修复：顶层 OR 链递归展平为全部原子备选，逐条求值保留，并选**最左 allow**。
2. **未知许可证 + 已知例外误判 deny**：`Totally-Unknown WITH Classpath-exception-2.0`
   原逻辑先做例外绑定检查，把"未知许可证不在绑定清单"判成 deny。
   这违背"未知不自动通过、也不武断拒绝"。修复判定次序为：
   许可证 deny→deny；**许可证 unknown→一律 unknown（例外不能消除未知性）**；
   许可证已知才进入例外未知/绑定/allow-deny 判定。

两处修复后均补充/更新了夹具与单测（`engine_cases.json` 与 `judge_test.go`）。

### 3.2 夹具测试：错误信息不准确（已修复）

`MIT AND`（运算符后直接 EOF）原报 `意外的记号 ""`，与夹具期望的"缺少许可证"不符。
解析器新增 EOF 分支，报"表达式意外结束，此处缺少许可证或左括号"。

### 3.3 验收脚本与环境冲突（已修复）

- 沙箱中 `127.0.0.1:18080` 被无关进程 `vccsim` 占用、`18091/18099` 也被占位服务
  占用（其中一个还会冒充返回 `{"status":"ok"}`），导致固定端口冒烟出现 404/误判。
  修复：`fixturetest` 改为**监听 `127.0.0.1:0` 取得内核临时端口**再启动子进程。
- 交付脚本 `examples/curl_examples.sh` 第一版有**作者自身 bug**：`call` 末尾
  追加 `"$BASE"` 而调用处已传完整 URL，造成对 `/` 的重复 404 请求；健康检查还
  硬编码 8080。已改为统一相对路径 + 可覆盖的 `BASE` 变量，复测 13×200 + 1×400。

### 3.4 明确的范围限制（非缺陷，未实现）

- 不支持 SPDX `+` 后缀运算符（词法层返回明确错误，有夹具）。
- 不做 SPDX 许可证列表版本校验、`-or-later` 语义归一化；按字面 ID 匹配策略。
- 缓存为文件系统 AST 缓存，非跨机共享；删缓存只影响性能。
- 本工具只做配置判定，**不作任何法律结论**。

## 4. 复现方式

```bash
go test -count=1 ./...
go run ./scripts/fixturetest -v        # 生成 reports/fixture-report.json
# 或手动：
go build -o bin/licensejudge ./cmd/licensejudge
bin/licensejudge serve --policy configs/policy.json --addr 127.0.0.1:8080 \
  --work-dir ./.work --cache-dir ./.cache
BASE=http://127.0.0.1:8080 bash examples/curl_examples.sh
```
