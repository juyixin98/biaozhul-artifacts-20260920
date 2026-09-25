# 自适应算术编码 · 码流格式规范（FORMAT v1）

本文档是 `adaptive-arith` 编解码器产出/消费码流的**完整比特级规范**。
任何符合本规范的实现均可与本实现互操作。规范是确定性的：同一输入必产生同一输出。

## 1. 符号表

| 符号编号 | 含义 |
|---|---|
| 0 ..= 255 | 字节值 0x00 ..= 0xFF |
| 256 | EOF 终止符（`EOF_SYMBOL`） |

共 257 个符号。每条消息以 EOF 符号显式终止；码流不携带显式长度。

## 2. 自适应模型（order-0）

编码器与解码器各自维护一份模型，按**完全相同**的规则同步更新：

1. **初始状态**：所有 257 个符号的频率均为 1，总频率 `total = 257`。
2. **更新**：每处理一个符号 `s`（包括 EOF），`freq[s] += 1`，`total += 1`。
   更新发生在该符号的区间细分**之后**（即编解码符号时使用的是更新前的频率）。
3. **重标定（rescale）**：当更新后 `total >= 16383`（`MAX_TOTAL_FREQ`）时，
   立即对所有符号执行 `freq[i] = (freq[i] + 1) >> 1`（折半向上取整，保证最小为 1），
   `total` 重新求和。重标定时机与规则固定，双方无需交换任何额外信息。

## 3. 算术编码器（32 位整数区间）

区间 `[low, high]` 为 32 位无符号整数（实现中用 u64 保存、恒限制在低 32 位）：

```
TOP_VALUE = 0xFFFFFFFF   FIRST_QTR = 0x40000000
HALF      = 0x80000000   THIRD_QTR = 0xC0000000
```

初始 `low = 0`，`high = TOP_VALUE`，`bits_to_follow = 0`。

### 3.1 编码一个符号 s

设 `cum` 为 s 的累计频率下界，`f = freq[s]`，`total` 为当前总频率（均为更新前的值）：

```
range = high - low + 1
high  = low + (range * (cum + f)) / total - 1
low   = low + (range * cum)       / total
```

除法为整数除法（向下取整）。随后执行归一化（3.3），最后按 §2 更新模型。

### 3.2 解码一个符号

设 `value` 为 32 位码值寄存器（初始预载码流的前 32 个比特，高位在前）：

```
range = high - low + 1
cum   = ((value - low + 1) * total - 1) / range
```

取累计频率区间包含 `cum` 的符号 s（`cum_low <= cum < cum_high`），然后：

```
high = low + (range * cum_high) / total - 1
low  = low + (range * cum_low)  / total
```

执行归一化（3.3），按 §2 更新模型。若 s 为 EOF，解码结束。

### 3.3 归一化与进位处理（bits-plus-follow）

编码器与解码器使用**相同**的循环条件；编码器写出比特，解码器读入比特：

```
loop:
  if high < HALF:                       # E1：区间在上半区之下
      编码器: 输出 0，再输出 bits_to_follow 个 1；bits_to_follow = 0
      解码器: （无区间调整）
  elif low >= HALF:                     # E2：区间在上半区
      编码器: 输出 1，再输出 bits_to_follow 个 0；bits_to_follow = 0
      low -= HALF; high -= HALF         # 解码器同时 value -= HALF
  elif low >= FIRST_QTR and high < THIRD_QTR:   # E3：区间跨越中点
      bits_to_follow += 1               # 进位未决，延迟输出
      low -= FIRST_QTR; high -= FIRST_QTR   # 解码器同时 value -= FIRST_QTR
  else: break
  low  <<= 1
  high  = (high << 1) | 1
  解码器: value = (value << 1) | 读入的 1 比特
```

进位处理要点：E3 条件下不确定的比特不立即输出，而是记为 `bits_to_follow`；
当 E1/E2 确定了实际比特 `b` 后，先输出 `b`，再输出 `bits_to_follow` 个 `¬b`。
解码器无需感知跟随位——它只需按相同条件移位读比特，码流自然对齐。

## 4. 终止与收尾

1. 编码器把 EOF 符号当作普通符号编码（含模型更新）。
2. 收尾：`bits_to_follow += 1`；若 `low < FIRST_QTR` 输出比特 0，否则输出比特 1；
   随后输出全部跟随位（按 §3.3 规则）。
3. 最后一个字节不足 8 位时**补 0 比特**。
4. 解码器解出 EOF 符号即停止；末尾的填充 0 比特不会被消费。

## 5. 比特序与字节序

- 字节内比特序：**MSB-first**（先写出的比特占据字节的 bit 7）。
- 码流为字节序列，不涉及多字节整数的端序问题（格式中无整数字段、无头、无校验）。

## 6. 截断与损坏流的判定

- 解码器在输入耗尽后按 0 比特继续补读，但补读总数超过 `max_zero_bits`
  （默认 64，实现 `Limits::max_zero_bits`）即判定**截断流**并报错。
  合法码流在 EOF 前所需的补读比特至多为 32（寄存器预载宽度），故该界限安全。
- 码流被篡改（非截断）时，解码输出为无意义字节，但输出长度恒受
  `max_output_bytes` 限制，解码必然有限终止，不会 panic、不会无限循环。

## 7. 资源限制

| 限制 | 参数 | 默认值 |
|---|---|---|
| 编码输入最大字节数 | `Limits::max_input_bytes` | 64 MiB |
| 编码/解码输出最大字节数 | `Limits::max_output_bytes` | 64 MiB |
| 输入耗尽后补读零比特上限 | `Limits::max_zero_bits` | 64 |

编解码均为流式：固定 8 KiB I/O 缓冲 + 约 2 KiB 模型表，内存占用 O(1)，
与数据长度无关。

## 8. 确定性

同一明文 + 同一版本规范 ⇒ 同一码流字节序列。模型初始状态、更新规则、
重标定规则、收尾规则、比特序全部固定，无任何随机或平台相关行为。
