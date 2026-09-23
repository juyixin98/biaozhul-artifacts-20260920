# 运行记录（RUNLOG）

本文件**如实记录**开发/验收过程中实际执行的命令与结果，包括开发中
踩到并修复的问题。环境：

- 日期：2026-09-23
- 系统：Linux 6.8.0-90-generic x86_64 (Ubuntu)
- Python：**3.12.3**（仅标准库，无第三方依赖）
- 工作目录：`/home/admin/Downloads/biaozhul/opp57/a`

所有命令都在仓库根目录执行；复现：`./acceptance.sh`。

---

## 1. 一键验收（最终结果）

命令：

```bash
./acceptance.sh
```

结果（结尾汇总）：

```
PASS=26 FAIL=0
```

26 项检查全部通过，覆盖：4 个示例编译、4 个示例验证、4 组程序输出、
7 个手工非法模块被拒、完整测试套件、4 个示例 targeted 变异无不变量
破坏、极小函数穷尽变异无不变量破坏。

---

## 2. 自动化测试

命令：

```bash
python3 -m unittest discover -s tests
```

结果：

```
Ran 66 tests in 1.93s

OK
```

测试文件与内容：

| 文件 | 数量 | 覆盖 |
|------|------|------|
| `tests/test_frontend.py` | 13 | 词法 token/位置/注释/非法字符；语法优先级、结合性、诊断 |
| `tests/test_compiler_verify.py` | 23 | 7 类合法程序验证、二进制往返、13 个手工非法字节码、最短路径与回边 |
| `tests/test_interpreter.py` | 11 | 循环/提前返回/递归/bool/向零截断除法/除零/燃料/递归深度/拒绝未验证模块 |
| `tests/test_mutation.py` | 11 | 四示例变异安全、越界/回边/返回/未初始化覆盖、单字节契约、穷尽变异 |
| `tests/test_service.py` | 8 | compile/verify/run/mutate/base64 函数级 + 真实 HTTP socket 端到端 |

---

## 3. 合法程序实际输出

```
$ python3 -m slang run examples/sum_loop.sl
55
[main 返回 void, 145 步, 最大栈高 2]

$ python3 -m slang run examples/early_return.sl
-1
0
1
120

$ python3 -m slang run examples/bool_logic.sl
true
false
true
false
11

$ python3 -m slang run examples/uninit_read.sl
42
7
```

语义抽查：`-7 / 2 == -3`、`-7 % 2 == -1`（向零截断），由
`test_truncating_div_and_mod` 断言。

---

## 4. 手工非法字节码（不经过编译器）

命令：

```bash
python3 examples/make_bad_modules.py
for f in examples/bad/*.bin; do python3 -m slang verify "$f"; done
```

7 个模块各自命中的首个错误码（实测）：

| 文件 | 错误码 | 含义 |
|------|--------|------|
| `bad_oob_jump.bin` | `JUMP_OUT_OF_BOUNDS` | 跳转目标 pc=100 越过代码长度 3 |
| `bad_backedge.bin` | `JUMP_UNALIGNED` | 回边目标 pc=1 落在 PUSH 操作数中间 |
| `bad_stack_merge.bin` | `STACK_MERGE_CONFLICT` | 合流栈高度 2 与 1 |
| `bad_type_merge.bin` | `STACK_MERGE_CONFLICT` | 合流栈格 int 与 bool |
| `bad_uninit.bin` | `LOCAL_UNINITIALIZED` | 直接 LOAD 未赋值槽 |
| `bad_underflow.bin` | `STACK_UNDERFLOW` | 空栈 ADD |
| `bad_retv.bin` | `RETURN_MISMATCH` | void 函数使用 RETV |

（另有测试覆盖 `TYPE_MISMATCH / BAD_SLOT / DECODE_ERROR(非法操作码
与操作数截断) / FALL_OFF_END / STACK_NOT_EMPTY /
LOCAL_UNINITIALIZED 在合流点`。）

越界跳转的可读路径示例：

```
[JUMP_OUT_OF_BOUNDS] 函数 f (#0) pc=0
  JUMP 目标 pc=100 越界（代码长度 3）
  最短错误路径:
  0. 进入函数 f @pc=0
  1. 到达 @pc=0: JUMP 目标 pc=100 越界（代码长度 3）
```

---

## 5. 单字节变异活动（验收核心）

命令（每个合法示例）：

```bash
python3 -m slang mutate examples/<name>.sl
```

实测统计（targeted 策略，仅改函数字节码区 1 个字节）：

| 示例 | 变异总数 | decode_rejected | verify_rejected | runtime_error | ran_clean | invariant/crash |
|------|---------:|---:|---:|---:|---:|---:|
| sum_loop | 380 | 0* | 297 | 13 | 70 | **0** |
| early_return | 716 | 0* | 506 | 23 | 187 | **0** |
| bool_logic | 777 | 0* | 567 | 5 | 205 | **0** |
| uninit_read | 252 | 0* | 201 | 1 | 50 | **0** |

\* “解码失败”计入 `verify_rejected` 的 `DECODE_ERROR` 错误码
（变异只改 code 区，外层信封始终能解；损坏指令在函数验证阶段报告
DECODE_ERROR）。分类键因此只出现 verify_rejected / runtime_error /
ran_clean 三类，外加绝不应出现的 invariant_broken / *_crashed。

`sum_loop` 的验证拒绝按错误码分布：

```
DECODE_ERROR        130     BAD_CALL_TARGET      4
BAD_SLOT             47     FALL_OFF_END         1
JUMP_OUT_OF_BOUNDS   43     JUMP_UNALIGNED       9
STACK_UNDERFLOW      24     LOCAL_UNINITIALIZED  9
STACK_MERGE_CONFLICT 12     RETURN_MISMATCH      3
STACK_NOT_EMPTY       8     TYPE_MISMATCH        7
```

### 穷尽变异（每字节全部 255 个异值）

命令：

```bash
python3 -m slang mutate /tmp/tiny.sl --strategy exhaustive
# /tmp/tiny.sl: fn main() { print 1; }
python3 -m slang mutate examples/sum_loop.sl --strategy exhaustive --func 1
```

结果：

| 目标 | 变异总数 | verify_rejected | runtime_error | ran_clean | invariant/crash |
|------|---------:|---:|---:|---:|---:|
| `print 1` 单函数 | 1275 | 764 | 0 | 511 | **0** |
| sum_loop 的 main | 1785 | 1274 | 122 | 389 | **0** |

`ran_clean` 表示变异体通过了验证并在燃料（默认 20000 步）内正常结束；
`runtime_error` 是除零/燃料耗尽/递归过深等**合法运行期**错误，
不是结构不变量破坏。

**结论：累计 3053 个穷尽变异 + 2125 个 targeted 变异，没有任何一个
变异体在“通过验证后”触发栈下溢或其它不变量破坏，验证器与解释器
均未崩溃。**

---

## 6. JSON 服务实测

命令：

```bash
python3 -m slang serve --port 8011 &
curl -s http://127.0.0.1:8011/health
curl -s -X POST http://127.0.0.1:8011/compile -H 'Content-Type: application/json' \
     --data @examples/requests/01_compile.json
# /verify、/run、/mutate、编译错误同理
```

实测响应摘录：

```
GET  /health  -> {"ok": true, "service": "slang-bytecode-verifier"}
POST /compile -> ok=True, nbytes=600, nfuncs=2
POST /verify  -> {"ok": true, "stage": "verify", "verified": true, "nfuncs": 2}
POST /run     -> {"ok": true, "printed": ["55"], "return": "void",
                  "steps": 145, "max_stack": 2}
POST /compile(错误源码) ->
  {"ok": false, "stage": "compile",
   "errors": [{"message": "期望 ';'，但遇到 '}'", "line": 3, "col": 1,
               "snippet": "}"}]}
POST /mutate  -> ok=True, count=50（limit 生效），含 label/offset/kind
```

---

## 7. 开发过程中出现并已修复的问题（如实记录）

1. **f-string 语法错误**：`f"... {x!r or y}"` 非法，改为先取
   `label = t.value or t.type.name`。修复后 parser 可用。
2. **参数语法口径**：初版示例误用 C 风格 `fn f(n: int)`，而语法
   定义是 `fn f(int n)`。统一为文档化的 `类型 名字` 形式
   （见 `docs/LANGUAGE.md`），示例同步修正。
3. **槽分配顺序错误**：最初编译器在生成初始化式之后才追加局部槽，
   导致 `STORE #1` 发出时验证器只看到 1 个槽（`BAD_SLOT`），且
   void 调用后误发 `POP`。修复：显式类型声明先占槽再生成初始化式；
   表达式语句只在非 void 时 `POP`；`print` 不再额外 `POP`
   （PRINT 自身消费栈顶）。
4. **布尔常量无法与 int 区分**：最初 `true/false` 复用 `PUSH imm`，
   验证器无法区分栈格类型。新增 `TRUE(0x1C)/FALSE(0x1D)` 两条
   无操作数指令，编译器、验证器、解释器、文档同步更新。
5. **条件跳转极性反了**：JIF 语义是“真则跳”，初版 `if/while`
   代码生成按“假则跳”布置标签，导致 `classify(-7)` 返回错误值。
   重写 `gen_if/gen_while` 的标签布局后，全部输出正确
   （55 / -1 0 1 / 120）。
6. **CLI 参数名与 argparse 属性冲突**：`--func` 被存为 `args.func`，
   与子命令处理函数属性重名，mutate 拿到的是函数对象。改 `dest`
   为 `func_index`。
7. **两个测试自身的构造笔误**（非产品代码）：漏导入 `OP_STORE`、
   把 `len(code)` 与 bytes 相加。修复后 66 个测试全绿。

---

## 8. 已知边界 / 刻意取舍（非缺陷，未通过项：无）

- 无第三方依赖，因此没有 `pip install`、`requirements.txt` 或锁文件；
  在干净的 Python 3.12 上直接运行，**未观察到环境相关失败**。
- 变量作用域刻意简化为**函数级**（无块级遮蔽），重复声明在前端报错。
- 整数**立即数**受指令格式限制为 16 位 `[-32768,32767]`；运行期
  计算结果不受此限。
- `&& / ||` 直接映射为 AND/OR 指令，**不做短路**（两侧都求值），
  语言文档已明示。
- 变异器只改函数字节码区（信封不变），因此“魔数损坏/尾部截断”
  没有出现在变异活动分类中；但解码路径由测试（非法操作码、操作数
  截断）和 `decode_module` 的健壮性检查覆盖。
- 本轮没有发现未通过的验收项；若未来在更大规模穷尽变异中发现
  `invariant_broken`，`acceptance.sh` 会以 FAIL 明确暴露。
