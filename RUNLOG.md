# 运行记录（RUNLOG）

本文件如实记录本项目在交付环境中的实际运行命令与结果。环境：
Linux 6.8.0-90-generic，Rust 1.98.1（cargo 1.98.1），x86_64。

## 1. 构建与静态检查

```text
$ cargo build
    Finished `dev` profile [unoptimized + debuginfo] target(s)

$ cargo build --release
    Finished `release` profile [optimized] target(s) in 1.57s

$ cargo clippy --all-targets
（无任何 warning/error 输出）
```

## 2. 自动化测试

```text
$ cargo test
running 16 tests          # src 内单元测试
test result: ok. 16 passed; 0 failed; ...

running 14 tests          # tests/end_to_end.rs 端到端/故障注入/HTTP
test result: ok. 14 passed; 0 failed; ...
```

单元测试（16）覆盖：CRC-32 标准向量、复合键解析与升降序比较、重复键稳定性、
行读取器（`\r\n`、空行、超长单行缓冲增长）、清单序列化/损坏拒绝、fan-in 预算钳制、
归并计划轮次、query/百分号解码。

端到端测试（14）覆盖：

| 测试 | 内容 |
|---|---|
| `empty_input_produces_empty_output` | 空输入 → 空输出，0 分段 |
| `forced_multi_round_merge_matches_reference` | 400 行、极小内存，≥8 分段、≥2 轮，对照内存参考排序 |
| `duplicate_keys_keep_input_order` | 重复键保持输入顺序 |
| `descending_and_composite_order_matches_reference` | `2:desc;1:asc` 对照参考排序 |
| `oversized_single_line_is_sorted_correctly` | 20KB 单行远超 mem=2048，正确排序 |
| `recovers_from_injected_sync_failure_in_run_phase` | 第 2 次分段 fsync 注入失败，恢复后仅复用故障前 1 个分段，结果正确 |
| `recovers_from_injected_sync_failure_in_merge_phase` | 归并中途 fsync 失败，前缀归并输出复用，结果正确 |
| `resumes_from_partial_merge_prefix` | 手工保留 2 个归并文件、截断清单，恢复后复用前缀 |
| `corrupted_run_file_is_detected_and_rebuilt` | 翻转分段载荷字节，verify_run 报 CRC 错，重跑重建成功 |
| `trailer_corruption_is_detected` | 翻转 trailer 字节，verify_run 报错 |
| `leftover_temp_file_is_cleaned_and_sorting_still_works` | 遗留 `*.tmp` 启动清理 |
| `injected_read_failure_can_be_resumed` | 输入读取到指定字节注入错误，恢复成功 |
| `conflicting_config_on_resume_is_rejected` | 不同配置恢复返回冲突错误 |
| `http_sort_resume_and_headers` | HTTP 提交/恢复、统计头、healthz、404、400 |

## 3. 一键验收脚本

`bash scripts/acceptance.sh`（退出码 0），关键实测结果：

```text
== 小内存多轮归并，对照内存参考排序 ==
X-Sort-Records: 1200
X-Sort-Runs: 145
X-Sort-Merge-Rounds: 3
X-Sort-Merge-Steps: 31
X-Sort-Runs-Reused: 0
X-Sort-Merges-Reused: 0
X-Sort-Fanin: 6
X-Sort-Peak-Bytes: 1792
X-Sort-Resumed: 0
PASS: multi: runs=145 (>=10)
PASS: multi: merge rounds=3 (>=2)
OK: 1200 records match in-memory stable reference (key: 1:asc;2:asc)
```

数据规模：1200 行、约 180KB；`mem=2048, buf=256`。内存预算强制产生 **145 个分段、
3 轮归并、31 个归并步骤**，有效 fan-in 被预算钳制为 6，报告峰值 1792 字节
（< 2048 预算）。`extsort verify` 与内存稳定参考排序逐字节一致。

脚本汇总：`PASS=17 FAIL=0`，逐项包括：cargo build/test、healthz、多轮归并对照、
GET 幂等重放、空输入、300KB 超大单行、配置冲突 409、损坏分段被
`frame CRC mismatch (seq=10)` 检出且重跑恢复、遗留临时文件清理。

## 4. 手工 HTTP 演示（复合键、错误码、稳定性）

复合键 `1:asc;2:desc`：

```text
$ printf 'alpha\t3\nalpha\t1\nbravo\t2\nalpha\t2\n' | curl -X POST --data-binary @- \
    ".../sort?job=stab&mem=300&buf=128&key=1:asc;2:desc"
alpha	3
alpha	2
alpha	1
bravo	2
```

（4 行被 mem=300 切成 4 个分段、2 轮归并，fanin 被钳为 2。）

跨分段重复完整键的稳定性：

```text
$ printf 'a\t1\tFIRST\nb\t0\tzz\na\t1\tSECOND\nc\t9\tqq\na\t1\tTHIRD\n' | \
    curl -X POST --data-binary @- ".../sort?job=s2&mem=350&buf=128&key=1:asc;2:asc"
a	1	FIRST
a	1	SECOND
a	1	THIRD
b	0	zz
c	9	qq
```

错误码实测：不存在作业 `GET /sort?job=nope` → `404`；非法键
`key=9:sideways` → `400`；以不同键恢复已有作业 → `409`；
`mem=200&buf=128`（mem < 2·buf）→ `400 bad request: mem=200 too small`。

## 5. 未通过项 / 已知限制

- **无未通过测试**：单元 16、集成 14、验收 17 项全部通过，clippy 零警告。
- 已知边界（设计取舍，非缺陷）：
  - 手写 HTTP 仅用于本地验证：每连接单请求、`Connection: close`、请求体 2 GiB 上限，
    不应暴露到不可信网络。
  - `X-Sort-Peak-Bytes` 是按记录估算的峰值（含归并缓冲），不是精确进程 RSS。
  - 归并游标选择用线性扫描（k 通常 ≤8）；超大规模可换 loser tree，不影响磁盘格式。
  - 行按字节序比较；非 UTF-8 字节也可排序，但按制表符取字段时以原始字节切分。
