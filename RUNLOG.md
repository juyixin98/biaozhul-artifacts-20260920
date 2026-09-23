# RUNLOG — 实际运行记录

环境：Linux x86_64（内核 6.8），`cargo 1.98.1` / `rustc 1.98.1`，**零第三方依赖**
（仅 Rust 标准库）。以下命令与结果均在本机实际执行；金额/字节为真实输出。

## 1. 构建 / 静态检查

| 命令 | 结果 |
|---|---|
| `cargo build --release` | `Finished release profile`，无警告 |
| `cargo test` | **42 通过 / 0 失败**（27 单元 + 15 端到端） |
| `cargo clippy` | **0 warning / 0 error** |
| `cargo fmt --check` | 通过 |

## 2. 多轮归并 + 对照内存参考排序

数据：20,000 行复合列 CSV（约 350 KiB），列 `(group,name,input_seq)`。
内存预算 **160 KiB**（接近下限 132 KiB），车道数 2：

```
$ extsort sort --input big.csv --output big.sorted \
    --spec 1:asc,2:asc --budget 163840 --lanes 2 --keep-temp
done: records=20000 level0_runs=17 merge_passes=5 merge_batches=20 output_bytes=358868
```

- map 阶段产生 **17 个 level-0 段**；
- 归并 **5 轮（多轮，非单轮）**；
- 与一次性驻留内存的参考排序（相同复合键 + 输入序号 tie-break）逐字节比对：
  `MATCH: external == in-memory reference`。

复合键升/降序 + 稳定性（`--spec 1:asc,2:desc`，重复键的第 3 列显示原始序号）：

```
1,bb,1   1,bb,6   1,aa,0   1,aa,3   1,aa,4   2,zz,2   2,zz,5
```

组 1 内 name 降序（bb 在 aa 前），两个 `1,bb` 保持输入序 1→6，
三个 `1,aa` 保持 0→3→4；与参考一致。

## 3. 空输入 / 无尾换行 / 超大单行

| 场景 | 结果 |
|---|---|
| 空输入 | `output.txt` 0 字节；`records=0` |
| 仅一行、无尾换行 `only,line` | 输出 `only,line\n`（统一补换行） |
| CRLF 输入 | `\r` 在行尾被剥离，规范化为 LF |
| **超大单行**：300,000 字节的 value，键为第 1 列 | 逐块流式，首行 300,003 字节无损，`zz,second` 紧随其后；进程驻留内存不随行长增长 |
| 普通记录与巨型行混排（3,000 行 + 90 KiB 巨行） | 与内存参考逐字节一致 |
| 整行键（默认）遇到 >头窗口 的大行 | 明确失败 `key_too_large`，不悄悄超预算 |

## 4. 故障注入与恢复

故障通过 `--fault`（CLI）或 `X-Fault-Inject`（HTTP）注入，语法
`kind[:needle[:count]]`，kind ∈ create/write/sync/rename/read_fail。

**map 阶段写失败后恢复：**

```
$ extsort sort ... --fault "create_fail:run-00008"
error: injected fault: create_fail on jobs/J/level0/run-00008.run.tmp   (退出码 1)
# manifest：state=map，run run-00000..run-00007 已提交；残留 .run.tmp
$ extsort resume --root ./rf --job J
resumed to done: records=20000 ... resumed=true
# 结果与内存参考逐字节一致：RECOVERY MATCH
```

**merge 阶段（以及“后续批次”）失败后恢复：** 注入 `create_fail:m-1-` 与
`create_fail:m-1-00003`，resume 后均得到完整 20,000 行、与参考一致。
HTTP 路径同样验证：故障返回 `500 fault_injected`，随后
`POST /jobs/{id}/resume` 输出完整 20,000 行。

## 5. 临时文件损坏检测

| 破坏方式 | resume 时的结果 |
|---|---|
| 翻转已提交段第 120 字节 | `corrupt_segment … head chunk CRC mismatch` |
| 段尾截断 20/24 字节 | `corrupt_segment … first chunk truncated` |
| 覆盖前 2 字节魔数 | `corrupt_segment … bad magic header` |
| 崩溃遗留 `*.run.tmp` | 恢复时被 `cleanup_temps` 删除，不参与归并 |
| 预算 < 下限（132 KiB） | `budget_too_small`（400） |

## 6. HTTP 验证入口

`extsort serve --root DIR --addr 127.0.0.1:PORT`，脚本
`examples/http-requests.sh` 实测：`/healthz`、`POST /jobs`（复合键、小预算）、
`GET /jobs/{id}`、`GET /jobs/{id}/output`、故障 500、`POST /jobs/{id}/resume`
（恢复为 20,000 行）、`GET /jobs`、`DELETE /jobs/{id}` 全部符合预期。
无任何前端 / HTML。

## 7. 开发过程中真实出现并已修复的缺陷（如实记录）

1. **1 路归并不收敛导致死循环（无限生成 level）。**
   初版预算规划在内存紧张时把车道数降到 1；1 路归并“一进一出”不减少段数，
   引擎在 level1 → level2 → … 空转（曾观察到 `level415`，CPU 100%）。
   修复：车道数**强制下限为 2**，并把最低预算改为
   `固定缓冲 + 2×每车道 + 最小驻留额度 = 135168 B (132 KiB)`。

2. **归并批次中途失败会丢数据（恢复正确性 bug）。**
   初版每个归并批一完成就（a）在写输出前先把输入从内存 manifest `drain` 掉，
   并（b）逐批删除输入 run 文件。HTTP 故障路径会把“已 drain、批未提交”的
   内存状态随故障写回 manifest，导致恢复时漏掉最前两批输入
   （20,000 行只输出约 17,6xx 行，315,956 B vs 参考 358,868 B）。CLI 因故障即
   退出、不回写 manifest 而侥幸未暴露。
   修复：① 输出 run fsync 成功**之后**才从 manifest 移除该批输入；
   ② 输入文件推迟到**整轮切换层级**时统一回收；③ 记录 I/O 故障时只写
   `status.json`，不把可能处于非提交点的内存 manifest 落盘。
   新增回归测试 `injected_merge_fault_on_a_later_batch_then_resume_completes`
   （故障点在已提交多批之后），修复后 CLI 与 HTTP 两条路径均逐字节对齐参考。

## 8. 复现实验的一键脚本

- `examples/cli-examples.sh`：造数、小内存多轮归并、对照 Python 稳定排序、
  空输入、300 KiB 巨行、故障+恢复、段损坏检测（实测全部通过）。
- `examples/http-requests.sh`：对运行中的服务执行上述 HTTP 调用（实测通过）。
