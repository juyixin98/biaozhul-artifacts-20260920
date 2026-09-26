# CDC1 二进制格式规范

版本：**1**（字段 `version = 1`）。本文档是 `.cdc` 文件的权威格式说明，实现见
`src/writer.rs`（编码）、`src/reader.rs`（解码）。

所有多字节整数均为**小端**；变长整数使用 **LEB128 / unsigned base-128**
（见下）。所有字符串均为 **UTF-8 字节序列**，不加 NUL 结尾。

## 1. 顶层文件布局

```
+------------------+
| magic   = "CDC1" |   4 字节，固定 0x43 0x44 0x43 0x31
| version= u8      |   固定 1
| flags   = u8     |   固定 0（保留；非 0 的文件将被拒绝）
+------------------+
| segment frame #0 |
| segment frame #1 |
|      ...         |
+------------------+
```

- 文件**没有尾部总长度或总段数字段**：段一直排列到 EOF。解码器以“读到 EOF”为
  正常结束信号；读到任何非完整帧都判为截断错误。
- 段编号（见下）在一个文件内必须**严格递增**（不要求连续）。合并器输出为单段，
  其编号为 0。
- 允许 0 个段（空列）：文件仅由 6 字节头构成。

## 2. 段帧（segment frame）

```
kind       : u8                 固定 1 = 字符串字典编码段
body_len   : varint             body 的字节数（不含本头与尾部 CRC）
body       : body_len 字节      见第 3 节
crc32      : u32 小端           对 [kind, body_len 的 varint 字节, body] 整体计算
```

- `crc32` 使用 IEEE 802.3 多项式（反射多项式 `0xEDB88320`，初值
  `0xFFFFFFFF`，结果取反），等价于 zlib/PKZIP 的 CRC-32。
- 校验范围**包含** `kind` 与 `body_len`，因此帧长度、类型与正文任一比特翻转
  都会被检出。校验失败返回 `CrcMismatch`，不会继续解析。
- 解码器在分配 body 缓冲**之前**先比较 `body_len` 与 `max_segment_bytes`，
  超限直接拒绝，从而保证损坏/恶意的巨大长度字段不会导致无界分配。

## 3. 段正文（body）

一个段对该段内的若干行做字符串字典编码。**NULL 独立表示，绝不进入字典；
空字符串 `""` 是字典中的普通条目。**

```
segment_no  : varint               段编号（文件内严格递增）
row_count   : varint               本段行数（含 NULL）
cardinality : varint               字典条目数 d（不含 NULL）

repeat d times:                    字典：ID 从 0 开始，按顺序排列
    entry_len : varint             该字符串的 UTF-8 字节数
    entry     : entry_len 字节     字符串内容（entry_len=0 即空字符串）

null_count  : varint               NULL 行数 n
repeat n times:
    null_pos  : varint             NULL 行的行号，在 [0, row_count) 内，
                                   必须严格升序

repeat (row_count - n) times:      非 NULL 行的字典 ID
    id        : varint             取值在 [0, cardinality)
```

### 3.1 行的排布规则（重要）

行按行号 `0..row_count` 排列。NULL 行**只**出现在 `null_pos` 列表中；
非 NULL 行的 ID 按行号顺序、**剔除 NULL 行后**连续排列。解码第 `r` 行：

1. 若 `r ∈ null_pos`，该行是 NULL；
2. 否则令 `k = |{ p ∈ null_pos : p < r }|`（r 之前的 NULL 个数），
   则该行的字典 ID 是 ID 序列中的第 `r − k` 个。

这样 NULL 不需要任何 ID 值，也不会占用字典空间。

### 3.2 字典 ID 语义

- 字典 ID 是**段内局部**编号：同一段内“相同字节 ⇔ 相同 ID”。
- 不同段之间的 ID **互相独立**，同样的字节在不同段可以有不同 ID。
- 跨段统一需经合并（见第 4 节）。
- ID 仅在 `[0, cardinality)` 内有效；越界即 `DictIdOutOfRange`。

### 3.3 合法性约束（解码器全部强制校验）

- `null_count ≤ row_count`；每个 `null_pos < row_count` 且严格升序；
- 每个行 ID `< cardinality`；
- 每个字典条目必须是合法 UTF-8；
- body 解析结束后位置必须恰好等于 `body_len`（不允许多余字节）。

## 4. 分段合并（merge）

合并输入为一个或多个 `.cdc` 文件中的若干段，输出为**一个新文件**，正文是
**单个段**（`segment_no = 0`），含一份**全局去重字典**。

映射以**字节相等**为唯一锚点，与输入字典顺序无关：

```
对每一段 s，按文件顺序、行号顺序处理每一行：
  若该行是 NULL：记一个全局 NULL，全局行号 = 段内行号 + 之前各段行数之和
  否则：
      bytes      := s.local_dict[local_id]
      global_id  := global_dict.intern(bytes)   # 相同字节全局唯一
      输出行 id  := global_id
```

- 全局字典编号顺序有两种，由请求中的 `canonical` 选择：
  - `canonical = false`（默认）：按“跨段首次出现顺序”编号；
  - `canonical = true`：按 UTF-8 **字节序**排序后编号。
- **字典顺序不影响解码结果**：两种顺序下，每个全局 ID 解析出的字节集合相同，
  合并前后**逐行值完全一致**；改变的只是同一个字节对应的整数编号。
- 合并是有界批量操作（需要见到全部输入才能确定全局字典），受
  `max_distinct` / `max_dict_bytes` / `max_rows` 限制。

## 5. LEB128（base-128 varint）

每字节低 7 位为载荷，最高位为续位标志：

- 小端组包：先出现的字节承载最低 7 位；
- 最高位 = 1 表示后续还有字节，= 0 表示结束；
- `0` 编码为单字节 `0x00`，`128` 编码为 `0x80 0x01`；
- u64 编码最长 **10 字节**，且第 10 字节只允许最低 1 位载荷；
  超长或溢出判为 `BadVarint`。

示例：

| 值   | 字节                       |
|------|----------------------------|
| 0    | `00`                       |
| 127  | `7f`                       |
| 128  | `80 01`                    |
| 300  | `ac 02`                    |

## 6. 内存与输出长度限制（有界性）

| 侧   | 限制项                    | 触发错误 |
|------|---------------------------|----------|
| 编码 | 单段缓冲字节 `max_segment_bytes` | 自动切段；单段仍超限则 `MemoryLimitExceeded` |
| 编码 | 单段行数 `max_rows_per_segment` | 自动切段 |
| 编码 | 单值字节 `max_value_len` | `ValueBytesLimitExceeded` |
| 编码 | 单段基数 `max_dict_cardinality` | `CardinalityTooLarge` |
| 解码 | 段帧 `max_segment_bytes` | `SegmentTooLarge`（在分配前判定） |
| 解码 | 累计行 `max_rows` | `RowsLimitExceeded` |
| 解码 | 累计输出字节 `max_value_bytes` | `ValueBytesLimitExceeded` |
| 解码 | 段字典字节 `max_dict_bytes` | `DictBytesLimitExceeded` |
| 解码 | 段基数 `max_cardinality` | `CardinalityTooLarge` |
| 解码 | 段数 `max_segments` | `TooManySegments` |
| 合并 | 全局基数 / 全局字典字节 / 行数 | 对应错误 |

编码与解码都只在内存中保留**至多一个段**，因此内存占用由
`max_segment_bytes`（编码侧为其保守缓冲估计）界定，与总行数无关。
