# 运行记录（run_results）

本文件如实记录开发过程中实际执行的命令、结果，以及中途发现并修复的问题。
环境：Linux 6.8，Python **3.12.3**，仅标准库，无第三方依赖。

## 1. 自动化测试

命令：

```bash
python -m unittest discover -s tests -v
```

最终结果：**66 个测试全部通过（OK），0 失败 0 错误，耗时约 5.5 秒。**

```
Ran 66 tests in 5.462s

OK
```

分布：

| 测试文件 | 测试数 | 覆盖内容 |
|---|---:|---|
| `tests/test_lexer_parser.py` | 22 | token、转义、行列位置、量词形式、语法错误、源码定位 |
| `tests/test_engine.py` | 22 | 连接/选择/括号/重复/锚点/`. `、码点语义、左起始最长、findall |
| `tests/test_exhaustive.py` | 3 | 与回溯解释器及 Python `re` 的穷举等价（万级样本） |
| `tests/test_catastrophic.py` | 6 | 参考解释器指数爆炸 + Thompson 线性 |
| `tests/test_service.py` | 13 | 请求处理函数 + 真实 HTTP 服务网络往返 |

### 穷举等价（核心验收）

- 由受限语法生成 **552 个模式**（覆盖连接、选择、括号、`{n}`/`{n,}`/`{n,m}`
  及 `*`/`+`/`?`），对字母表 `{a,b}` 上**全部**长度 0–4 的 **31 个字符串**比较。
- `fullmatch`：**17,112 对**比较，Thompson 引擎与回溯参考解释器布尔结果
  **完全一致，0 分歧，0 预算跳过**。
- `search`：**17,112 对**比较，`(start,end)` 跨度**完全一致，0 分歧**。
- 无锚点子集再与 Python 标准库 `re.fullmatch(..., DOTALL)` 交叉验证
  （>5000 对），**完全一致**。

> 说明：因本引擎 `$` 语义（严格文末，不在末尾 `\n` 之前匹配）与 Python `re$`
> 默认行为不同，与 `re` 的交叉验证只对**无锚点**模式做；含 `^`/`$` 的模式由
> 本引擎与自有参考解释器（锚点语义与引擎一致）对照。

### 灾难性回溯（核心验收）

命令：`python scripts/bench_backtrack.py`（原始 JSON 存于
`examples/bench_result.json`）。模式 `(a+)+b`，输入为 `a`×n（无 `b`，必败）：

| n | 回溯参考解释器递归步数 | Thompson NFA 边检查数 | Thompson 墙钟时间 |
|---:|---:|---:|---:|
| 8 | 1,800 | 115 | 0.00005 s |
| 10 | 7,178 | — | — |
| 12 | 28,684 | — | — |
| 14 | 114,702 | — | — |
| 16 | 458,768 | 251 | 0.00009 s |
| 18 | 1,835,026 | — | — |
| 20 | 7,340,052 | — | — |
| 32 | — | 523 | 0.00018 s |
| 64 | — | 1,067 | 0.00033 s |
| 128 | — | 2,155 | 0.00066 s |
| 256 | — | 4,331 | 0.00129 s |
| 1,000 | — | 16,979 | 0.0051 s |
| 10,000 | — | 169,979 | 0.051 s |
| 100,000 | — | 1,699,979 | 0.53 s |

- 参考解释器步数随 n **每 +1 约 ×4**（n 翻倍约 ×4，对应 2^(n+1) 条切分）；
  `test_reference_steps_grow_exponentially` 实测 n=14→15 步数比 >1.7。
- `(a?){25}a{25}` 在 24 个 `a`（少一个）上参考解释器 **200 万步预算内无法
  完成**（`BudgetExhausted`）；Thompson 同模式仍为线性。
- Thompson n 翻倍时边检查数比值 **2.084 / 2.04 / 2.02 / 2.01（趋近 2.0）**，
  断言阈值 ≤ 2.6；10 万码点输入 0.53 s 完成。**未出现指数退化。**

## 2. JSON 服务实测

启动：`python -m renfa.service --port 8080`，用
`bash examples/curl_examples.sh` 实测（完整输出存
`examples/curl_output.txt`）。关键响应：

```jsonc
// GET /healthz
{"ok": true, "engine": "renfa", "version": "1.0.0"}

// POST /match  fullmatch
{"ok": true, "op": "fullmatch", "matched": true,
 "match": {"start": 0, "end": 6, "text": "cddeff"}, "matches": []}

// POST /match  findall（Unicode，偏移为码点）
{"ok": true, "op": "findall", "matched": true,
 "matches": [{"start": 1, "end": 3, "text": "中中"},
             {"start": 4, "end": 5, "text": "中"}]}

// 语法错误 -> HTTP 400
{"ok": false, "error": {"type": "syntax",
  "message": "括号 '(' 没有对应的 ')'", "location": "1:2"}}
```

`debug:true` 会额外返回 tokens（含每个 token 的码点偏移与行列号）、AST（每个
节点带 span）、NFA（全部状态与边）。样例见 `examples/response_debug.json`。

## 3. CLI 实测

```
$ python -m renfa.cli match -p 'a(b|c)*' -t abbc --op search
{"matched": true, "match": {"start": 0, "end": 4, "text": "abbc"}}      # 退出码 0

$ python -m renfa.cli match -p 'a(b|c)*' -t xyz --op search
{"matched": false, "match": null}                                        # 退出码 1

$ python -m renfa.cli match -p '*abc' -t abc
正则语法错误: '*' 前面没有可重复的原子 (第 1 行, 第 1 列)
  |
  | *abc
  | ^                                                                    # 退出码 2

$ python -m renfa.cli match -p 'a{99999}' -t ''
NFA 状态数超过上限 20000：请减小重复次数                                  # 退出码 2

$ python -m renfa.cli match -p '(a)\1' -t aa
正则语法错误: \1 看起来是反向引用/数字转义；本引擎不支持反向引用 ...      # 退出码 2
```

## 4. 中途发现并修复的问题（如实记录）

开发自测时第一次冒烟测试整体挂起/失败，逐个定位并修复了以下问题：

1. **词法器死循环（真实 bug）**：`{n,m}` 量词扫描函数用局部指针推进，结束后
   没有回写 `self.pos`，导致主循环在同一个 `{` 上无限重复。现象：任何含
   `{...}` 的模式（如 `a{2,3}`）都让程序卡死。已在 `lexer.py` 补上
   `self.pos = end`，并由全套测试回归覆盖。
2. **参考解释器零宽无限重复递归**：初版 `_repeat` 的零宽守卫缩进/控制流写错，
   会在 `(a|)*` 等零宽体上无限递归。重写为"同一位置只向更深层展开一次"，
   语言层面与 Thompson NFA 等价；另把解释器递归上限提高到 20,000（步数预算
   仍是硬限制）。
3. **解析器单子连接多包一层 Concat**：`(ab)+` 的 AST 根被包成单子 `Concat`，
   使"顶层是 Repeat"的断言失败。改为单子连接直接返回该节点。
4. **错误信息中量词名称为空**：错误标签用符号 `'*'` 作 dict 的键，但实际键是
   token 类型常量，导致显示成"重复量词  前面…"。已修正映射。
5. **反向引用未显式拒绝**：初版把 `\1` 静默当字面字符 `1`。鉴于需求明确"不支持
   反向引用"，改为词法期对 `\1`–`\9` 报错（`\0` 仍作为空字符转义保留）。
6. **测试自身的若干错误**（非引擎问题）：多行 token 索引少算了 `\n`、
   组合字符用例源码误写成预组合 `é`（U+00E9 单码点）而非 `é`、
   交叉验证里误用 `re.DOTALL`（未走别名导入）、灾难性测试的步数预算阈值一度
   设在实测步数之上（n=12 实测 28,684 步，预算改为 25,000 才稳定触发）。

这些问题修复后，66 个测试全部通过。

## 5. 已知边界 / 未覆盖项

- 未做大小写不敏感、Unicode 规范化、字符类（`[a-z]`）等——均为范围外特性，
  遇到会带位置报错而非静默错误处理。
- 穷举文本限于字母表 `{a,b}`、长度 ≤4、模式深度 ≤2；这是"限定语法 + 短串"的
  系统性穷举，不声称覆盖任意长输入，但线性性由 NFA 模拟算法保证并有大输入基准。
- 服务为标准库 `ThreadingHTTPServer`，面向本地/演示用途，未加鉴权与限流；
  请求体限制 1 MiB、NFA 状态数限制 20,000。
- 无前端（按需求）。
