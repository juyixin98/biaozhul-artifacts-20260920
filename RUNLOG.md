# 运行记录（RUNLOG）

记录在本机对本项目实际执行过的命令与结果。环境：
`go version go1.22.2 linux/amd64`，Linux 6.8.0-90-generic。
全部命令均在**本地**执行，未连接任何云平台或外部网络服务。

> 说明：以下结果为开发当时的真实输出。时间戳、耗时（ms）等数值在
> 不同机器/不同次运行间会有差异。

## 1. 构建

命令：

```bash
go build ./...
go build -o /tmp/licensejudge ./cmd/licensejudge
```

结果：成功，无输出，退出码 0。

## 2. 静态检查

命令：

```bash
go vet ./...
gofmt -l ./cmd ./internal
```

结果：`go vet` 退出码 0、无告警；`gofmt -l` 无输出（全部文件已格式化）。

## 3. 自动化测试

命令：

```bash
go test ./... -count=1
```

结果（退出码 0，全部通过）：

```
?   licensejudge/cmd/licensejudge        [no test files]
ok  licensejudge/internal/api
ok  licensejudge/internal/judge
ok  licensejudge/internal/policy
ok  licensejudge/internal/runner
ok  licensejudge/internal/spdx
```

顶层 `--- PASS` 用例 18 个；其中 `judge` 的表驱动用例另含 22 个判定子用例，
`spdx` 含 13 个解析子用例。覆盖率（`go test ./... -cover`）：

```
internal/spdx   94.0%
internal/judge  93.8%
internal/api    77.4%
internal/policy 71.1%
internal/runner 63.2%
```

### 验收点与对应测试

| 验收点 | 测试 |
| --- | --- |
| 优先级 `OR < AND < WITH` | `TestParsePrecedence`、`TestWithPrecedence`、`precedence or-and chooses conjunction` |
| 括号分组 | `TestParseParentheses`、`TestParseNestedAndMixedCase`、`parentheses force grouping` |
| WITH 例外组合 / 组合覆盖 | `TestLeafDecision`、`with allowed combo`、`with denied combo override` |
| WITH 不能作用于括号组 | `(MIT OR Apache-2.0) WITH ...` 期望解析错误 |
| 非法表达式报错 | `TestParseNestedAndMixedCase`、`TestInvalidExpression` |
| AND 三值真值表 | `and both allowed/one denied/one unknown/denied plus unknown` |
| OR 三值真值表 | `or first/second branch allowed`、`or all denied`、`or unknown branch ...` |
| 未知不自动通过 | `unknown license is not auto-approved`、API `TestJudgeEndpoint` |
| 保留备选分支并输出一条选择 | `TestOrAlternativesPreserved`、`chain three alternatives middle allowed` |
| 拒绝原因 | `TestAllDeniedReportsAllBranchesDenied`、非 allow 结果必带 reasons |
| 夹具白名单拒绝 | `TestUnknownCommandRejected`、API `TestRunFixtureRejectsUnregistered` |
| work/cache 目录分离 | `TestRunWhitelistedAndCache`、`TestSameWorkAndCacheRejected` |
| 缓存命中 / 失败不缓存 | `TestRunWhitelistedAndCache`、`TestFailedCommandNotCached` |
| workdir 不得逃逸基目录 | `TestWorkDirEscapesBase` |

开发过程中出现过 1 次测试失败，已修复：初版 OR 备选分支错误地把
被拒分支的决策取成整个 OR 的结果；改为对每个分支独立求值后，
测试期望同步更正为分支自身的真实决策（`deny`）。

## 4. 启动服务

命令：

```bash
/tmp/licensejudge -addr 127.0.0.1:8080 \
  -policy configs/policy.json -fixtures configs/fixtures.json \
  -work-dir .local/work -cache-dir .local/cache
```

启动日志（确认 work / cache 为不同绝对路径）：

```
work dir: /home/admin/.../a/.local/work
cache dir: /home/admin/.../a/.local/cache
licensejudge listening on http://127.0.0.1:8080
```

## 5. 请求样例

命令：

```bash
./examples/run_samples.sh
```

结果：14 个样例全部返回预期状态码——13 个 `200`，其中非法表达式
`MIT OR` 与未登记夹具 `rm -rf /` 返回 `400`。真实响应已保存到
`examples/responses/`。关键结果：

- 优先级：`GPL-3.0-only OR MIT AND Apache-2.0` → `allow`，
  `selection = "MIT AND Apache-2.0"`（OR 的左备选 deny、右备选 allow）。
- 括号：`(GPL-3.0-only OR MIT) AND Apache-2.0` → `allow`。
- 例外组合允许：`GPL-2.0-only WITH Classpath-exception-2.0` → `allow`。
- 例外组合覆盖拒绝：`GPL-3.0-only WITH Classpath-exception-2.0` → `deny`。
- 全部备选被拒：`GPL-3.0-only OR AGPL-3.0-only` → `deny`，
  原因码 `all-branches-denied`。
- 未知许可证：`WeirdProprietary-9.9` → `unknown`（**未自动通过**）。
- 未知 + 被拒并存：`GPL-3.0-only OR BrandNewLib-0.1` → `unknown`，
  备选决策 `[deny, unknown]`（存在未知项时转人工审查而非直接拒绝）。

## 6. 经白名单端点运行夹具命令

```bash
POST /v1/fixtures/run {"name":"go-vet"}   -> exit_code 0
POST /v1/fixtures/run {"name":"go-test"}  -> exit_code 0，5 个包 ok
POST /v1/fixtures/run {"name":"go-build"} -> exit_code 0
POST /v1/fixtures/run {"name":"go-version"}
    首次 cached:false，stdout "go version go1.22.2 linux/amd64"
    再次        cached:true
    no_cache:true cached:false
```

未登记命令 `{"name":"rm -rf /"}`：

```
HTTP 400 fixture-not-allowed
"unknown fixture command \"rm -rf /\"; allowed: [go-build go-test go-version go-vet true]"
```

缓存文件仅出现在 `.local/cache/`；`.local/work/` 为空——
确认缓存目录与工作目录分离。失败命令（`false`）不写缓存。

## 7. 未通过项 / 已知限制

- 无未通过的构建、vet 或测试项。
- 非目标（未实现，属设计限制而非缺陷）：非 SPDX 全量实现，
  不校验官方许可证列表、不支持 `+` 与 `LicenseRef`；不做法律结论；
  服务默认仅监听回环地址，未包含鉴权/ TLS（定位为本地工具）。
