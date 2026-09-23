# 分区哈希连接（Partitioned Hash Join）— 单机内存查询引擎

纯后端、零第三方依赖的 Java 单机查询引擎。核心连接运算（哈希、分区、落盘、
回退）全部手写，**不调用任何现成 SQL 引擎**。通过 JSON 请求驱动，支持执行计划
与结果数据导出。

## 功能范围

- **连接类型**：`INNER`（内连接）、`LEFT`（左外连接）。
- **多列连接键**：任意列数的复合键。
- **NULL 语义（SQL 标准）**：键中任一列是 NULL 即“空键行”，NULL 永不等于任何值
  （也不等于另一个 NULL）。INNER 丢弃两侧空键行；LEFT 将未匹配（含空键）的左行
  以右侧全 NULL 补齐输出。
- **重复键笛卡尔积**：构建侧每个键保存桶列表，每个探测行与桶内全部构建行配对，
  自然产生重复键的多重集笛卡尔积。
- **超内存阈值分区落盘**：两侧任一超过 `inMemoryRows` 即进入分区模式，按深度加盐
  哈希将两侧写为成对的磁盘分区文件，再逐对处理。
- **热点分区处理**：
  - 构建侧仍超过阈值且重分区深度未到上限 → 用**新的哈希种子递归再分区**
    （均匀键会被拆开；单一热点键哈希后仍在同一块，拆不开）；
  - 达到深度上限后 → **有界块嵌套循环回退（bounded block-nested-loops）**：
    构建侧与探测侧都按不超过 `inMemoryRows` 行的块流式扫描，峰值驻留内存有界，
    因此全热点键（所有行同一个键）无论多大都能算完。
- **磁盘额度**：`diskBudgetBytes` 实时计费，每次写入前检查；超限立即失败并返回
  `DISK_BUDGET_EXHAUSTED`，临时目录自动清理。
- **导出**：结果可直接内联在响应中，或以 JSONL / JSON 数组写文件；执行计划可单独
  导出（`plan` 子命令只做计划不执行，也可随运行一并导出）。
- **表数据来源**：请求内联 `rows`，或 `file` 引用外部 JSONL / JSON 文件。

## 环境与构建

- 需要 JDK 17+（仅用 `javac`/`java`，无需 Maven/Gradle，无外部依赖）。

```bash
./build.sh          # 编译主代码与测试 -> build/classes, build/test-classes
./test.sh           # 运行自动化测试套件
./run.sh <请求.json> [响应文件.json]   # 执行一次连接
./run.sh plan <请求.json>              # 只输出执行计划
cat req.json | ./run.sh -              # 从标准输入读取请求
```

退出码：`0` 成功；`2` 连接/请求错误（stderr 输出 `{"ok":false,"error":{...}}`）；
`1` 用法或本地 IO 错误。

## 请求格式

```json
{
  "joinType": "INNER | LEFT",
  "left":  {
    "name": "t1",
    "columns": ["k", "a"],
    "rows": [[1, "x"], [2, null]]
  },
  "right": {
    "name": "t2",
    "columns": ["k", "b"],
    "file": {"path": "data/t2.jsonl", "format": "jsonl"}
  },
  "leftKeys": ["k"],
  "rightKeys": ["k"],
  "options": {
    "inMemoryRows": 1024,
    "diskBudgetBytes": -1,
    "maxRepartitionDepth": 2,
    "spillDir": "/tmp/partjoin-spill",
    "maxInlineRows": 10000,
    "output": {"path": "out/result.jsonl", "format": "jsonl"},
    "planOutput": {"path": "out/plan.json"}
  }
}
```

选项说明：

| 选项 | 默认 | 含义 |
| --- | --- | --- |
| `inMemoryRows` | 1024 | 单侧驻留内存的行数阈值；也是回退算法的分块大小 |
| `diskBudgetBytes` | 无限（`-1`） | 落盘临时文件的实时字节额度，超限报错 |
| `maxRepartitionDepth` | 2 | 递归再分区最大深度，超过后走块嵌套循环回退 |
| `spillDir` | 系统临时目录下 | 落盘根目录，每次运行在其下建唯一 UUID 子目录，结束即删 |
| `maxInlineRows` | 10000 | 不导出文件时响应中内联返回的最大行数（其余仅计数） |
| `output` | 无 | `{path, format: jsonl|json}`，结果流式写文件 |
| `planOutput` | 无 | 计划（及随运行的统计）写文件 |

输出列顺序固定为 **左表全部列 + 右表全部列**；LEFT 未匹配行右侧补 NULL。

值类型与 SQL 风格相等规则：整数/浮点/十进制按**数值**比较（`1 = 1.0 = 1.00`），
字符串 `"1"` 不等于数字 `1`，布尔与数字互不相等。

## 算法说明

1. **空表短路**：右表为空时 INNER 无输出、LEFT 输出全部左行补 NULL；左表为空无输出。
2. **小输入**：两侧都 ≤ `inMemoryRows` 时直接内存哈希连接（INNER 用较小侧建表，
   LEFT 固定右内表建表，用位图记录已匹配的左行）。
3. **分区**：复合键先做逐组件的值哈希（数字归一化），再用深度加盐的 murmur3 风格
   混合函数取模分桶；fanout 按较大侧行数 / 阈值 × 1.3 估算，夹在 [4, 64]。
   空键行不进分区。左右两侧用相同哈希函数保证同键同行落同桶。
4. **逐桶处理**：桶对独立处理。INNER 在桶内仍允许选较小侧建表；LEFT 固定右侧建表
   以便用左行原始行号位图记录匹配。
5. **再分区/回退**：构建侧过大则递归再分区（深度 +1、换新种子）；深度耗尽走双侧
   分块的块嵌套循环，内存有界。
6. **落盘格式**：JSONL，每行 `{"row":[...],"id":<左行原始序号或 -1>}`；所有字节
   计入额度。

统计字段（响应 `stats`）：`mode`、输入/输出行数、两侧空键行数、创建/处理的分区数、
达到的最大再分区深度、再分区次数、回退次数、落盘文件数与累计字节数。

## 正确性验证

测试不使用 JUnit，自带极简断言框架。**核心做法是与独立的嵌套循环参考实现
（`NestedLoopJoin`）逐对比较键、按 JSON 规范化后比较输出多重集**（按行内容计频次，
而非只看行数）。覆盖：

- 值相等/哈希（跨类型数字、类型族隔离）、复合键 NULL 永不匹配；
- 基础 INNER / LEFT 补 NULL / 重复键笛卡尔积；
- 空表（左右四种组合 × 两种连接）、多列键各位置含 NULL；
- 多组随机数据（含 8% NULL、小键域制造重复）下内存与落盘模式对比参考实现；
- **全热点键** INNER 与 LEFT（3000×400 / 2500 热点 + 未匹配 + 空键）走回退，验证
  完整笛卡尔积与位图标记；
- 热点键 + 大量均匀键混合（回退与再分区并存）；
- 均匀大键被真正拆开（50k×50k，零回退）及 5 万级冒烟计时；
- **磁盘额度耗尽**：错误码、零输出、临时文件全部清理；
- JSON 解析/写入保真、计划接口、CLI 端到端（stdin、plan、额度失败退出码 2）。

## 目录结构

```
src/main/java/partjoin/
  Main.java            JSON 命令行入口（无 HTTP 服务）
  RequestRunner.java   请求解析、表加载、执行与响应/导出
  HashJoinEngine.java  分区哈希连接、递归再分区、块嵌套循环回退、统计
  NestedLoopJoin.java  嵌套循环参考实现（测试预言机）
  Spiller.java         落盘文件管理与磁盘额度计费
  Value/Row/Key/Schema/Table/JoinType/JoinException.java  数据模型
  Json.java            零依赖 JSON 解析/序列化
  RowCollector.java    内联/计数/流式文件输出收集器
src/test/java/partjoin/test/  自动化测试（17 个测试类）
examples/                请求样例与外部数据样例
build.sh test.sh run.sh  构建/测试/运行脚本
RUNLOG.md                实际运行命令与结果记录
```

## 限制（非目标）

- 无前端、无 HTTP server；入口是进程 + JSON（stdin 或文件）。
- 非等值连接、聚合、排序、投影裁剪、UPDATE/DELETE 等不在范围内。
- 内存计量以“行数阈值 + 分块”保证驻留有界，不做逐字节 JVM 堆核算；磁盘额度按
  落盘字节精确计费。
