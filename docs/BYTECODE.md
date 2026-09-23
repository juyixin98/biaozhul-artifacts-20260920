# 栈式字节码与验证器规范（BYTECODE）

本文件定义 Slang 的指令集、模块二进制格式，以及验证器的检查规则、
数据流算法与“最短错误路径”。

## 1. 指令集

操作码 1 字节；操作数宽度随操作码固定（因此线性扫描即可确定每条
指令的长度与边界）。多字节整数为大端序；`imm16/rel16` 为有符号数。

| 助记符 | 编码 | 操作数 | 栈效果（前 → 后） | 说明 |
|--------|------|--------|-------------------|------|
| PUSH   | 0x01 | rel:imm16 | → int | 压入有符号整数常量 |
| TRUE   | 0x1C | — | → bool | 压入布尔真 |
| FALSE  | 0x1D | — | → bool | 压入布尔假 |
| LOAD   | 0x02 | slot:u8 | → value | 读局部量槽 |
| STORE  | 0x03 | slot:u8 | value → | 写局部量槽 |
| POP    | 0x04 | a → | 丢弃栈顶 |
| DUP    | 0x05 | a → a a | 复制栈顶 |
| ADD/SUB/MUL/DIV/MOD | 0x06–0x0A | — | int int → int | 二元整数运算 |
| EQ/NE/LT/LE/GT/GE | 0x0B–0x10 | — | int int → bool | 整数比较 |
| AND/OR | 0x11–0x12 | — | bool bool → bool | 逻辑 |
| NOT    | 0x13 | — | bool → bool | 逻辑非 |
| NEG    | 0x14 | — | int → int | 一元负 |
| JUMP   | 0x15 | rel16 | 不变 | `pc += rel16`（rel 相对操作码自身） |
| JIF    | 0x16 | rel16 | bool → | 栈顶为真则跳，为假则顺序执行 |
| CALL   | 0x17 | func:u8 | args… → value/空 | 实参个数由被调函数签名决定 |
| RET    | 0x18 | — | （要求空栈） | void 返回 |
| RETV   | 0x19 | — | value → | 带值返回 |
| PRINT  | 0x1A | value → | 弹栈并打印 |
| NOP    | 0x1B | — | 不变 |

跳转偏移 `rel16` 相对**该跳转指令的操作码字节**，即
`目标pc = 操作码pc + rel16`，范围 `[-32768, 32767]`。

栈高上限 `MAX_STACK = 256`（验证期与解释期一致）。

## 2. 模块二进制格式

```
magic   = b"SLANGBC1"                 8 字节
name    : u16 长度 + UTF-8
source  : u16 长度 + UTF-8            内嵌源码（用于回显诊断）
nfuncs  : u16
func[nfuncs]:
    name       : u8 长度 + ASCII
    nparams    : u8
    ptype[k]   : 每参数 1 字节类型 (1=int,2=bool)
    ret_type   : 1 字节 (0=void,1=int,2=bool)
    nlocals    : u16
    ltype[j]   : 每局部槽 1 字节类型（参数槽也计入 nlocals）
    codelen    : u32
    code       : codelen 字节原始字节码
    nmap       : u16（调试映射条数）
    map[m]:
        pc     : u32
        start  : u32（源码偏移）
        end    : u32（源码偏移）
```

变异器只替换某函数的 `code` 区域，外层“信封”（含源码与调试映射）
原样重新编码——因此变异后的模块报错仍能映射回源码行列。

## 3. 验证器

验证器**不假设字节码由本项目的编译器产生**，对手工/变异字节码同样
适用。它在每个函数上执行单调数据流分析。

### 3.1 抽象状态（块头帧 Frame）

- `stack`：栈格序列，每格类型为 `int` 或 `bool`（栈元素总有类型）；
- `locals`：每槽三态——`uninit`（底）/`int`/`bool`。
  函数入口处参数槽按签名初始化为具体类型，其余槽为 `uninit`。

### 3.2 检查项与错误码

| 错误码 | 触发条件 |
|--------|----------|
| `DECODE_ERROR` | 非法操作码、操作数截断、模块信封损坏 |
| `JUMP_OUT_OF_BOUNDS` | 跳转目标 `<0` 或 `>= codelen` |
| `JUMP_UNALIGNED` | 跳转目标未落在某指令操作码边界上 |
| `STACK_UNDERFLOW` | 弹栈时栈为空（含 CALL 实参不足） |
| `STACK_OVERFLOW` | 栈高超过 256 |
| `TYPE_MISMATCH` | 运算/比较/逻辑/JIF 操作数类型不对 |
| `STACK_MERGE_CONFLICT` | 合流点栈高度不等，或同格类型不一致 |
| `LOCAL_MERGE_CONFLICT` | 合流点同槽被当作 int 又被当作 bool |
| `LOCAL_UNINITIALIZED` | LOAD 的槽在某条前驱路径上未赋值 |
| `BAD_SLOT` | LOAD/STORE 槽号 ≥ 局部槽数 |
| `BAD_CALL_TARGET` | CALL 函数号越界；实参类型不匹配也在此附近报 TYPE_MISMATCH |
| `STACK_NOT_EMPTY` | RET 时栈非空 |
| `RETURN_MISMATCH` | 带值/无值返回与函数签名不符，或值类型不符 |
| `FALL_OFF_END` | 控制流到达代码末尾却没有 RET/RETV/JUMP |

局部量合流规则（确定赋值）：

- `int ⊔ int = int`，`bool ⊔ bool = bool`；
- `uninit ⊔ T = uninit`（**一路可能没写，合流后即视为可能未初始化**，
  随后读取会被拒）；
- `int ⊔ bool` = 冲突。

栈合流规则：高度必须相等；逐格类型必须相等，否则冲突。

### 3.3 算法（worklist）

1. 线性解码，标记基本块 leader（入口 0、跳转目标、JIF 顺序后继）；
2. 入口帧入 worklist；
3. 从某 leader 的帧出发，逐条做抽象栈机执行，直到块终结
   （JUMP / JIF / RET(V) / 下一 leader / 末尾）；
4. 对每个后继做合流：首次到达直接放入；否则按 3.2 规则合并，
   发生变化则重新入队；
5. 任何检查失败立即返回结构化 `VerifyError`（含最短错误路径）。

终止性：栈格只在首次合流时定型；局部槽在三态格上只可能经历有限次
`uninit↔typed` 变化，且 `uninit ⊔ typed` 一旦落到 `uninit` 即固定，
因此迭代有上界。回边（向后跳转）作为普通合流处理，不需要额外机制。

### 3.4 最短可读错误路径

错误发生时，验证器在该函数的**结构化控制流图**上从入口 `pc=0`
做一次 BFS（边权均为 1），求出到错误指令的最短路径。只沿合法边
（通过边界/对齐检查的跳转）搜索，因此路径是执行时真实可能走到的。

路径节点为以下之一，并带 `pc` 与源码行列：

- `entry`：进入函数；
- `fallthrough`：顺序执行到下一条；
- `jump`：无条件跳转；
- `jif-taken` / `jif-not-taken`：条件跳的两个方向。

若错误点结构上不可达（如死代码中的问题），退化为入口单点，
错误信息仍给出精确 `pc`。CLI 用文本渲染，JSON 服务用
`shortest_error_path` 字段给出结构化数组。

## 4. “验证通过后不栈下溢”的保证与运行期断言

验证通过意味着：从入口出发的每条可达路径上，每条指令弹栈前栈中
都有足够、且类型正确的值，跳转都在边界内且对齐，读到的局部槽都已
初始化，CALL/RET 约定满足。因此解释器在验证通过的模块上：

- **不应发生栈下溢**；
- 不应读到未初始化槽；
- 不应遇到非法操作码/非边界 pc。

解释器仍保留防御性检查：一旦上述不变量被破坏，抛出
`InvariantBroken`（区别于除零、燃料耗尽等合法 `RuntimeErr`）。
变异活动（`slang/campaign.py`）在所有变异体上断言
`invariant_broken` 计数为 0。

## 5. 合法运行期错误（验证器不拦截）

这些依赖运行期数据或资源，验证器不负责，但解释器有确定行为：

- 整数除零 / 对零取模 → `RuntimeErr`；
- 执行步数超过 `fuel`（默认 1,000,000）→ `RuntimeErr`（捕获死循环）；
- 调用深度超过 200（捕获无限递归）→ `RuntimeErr`。
