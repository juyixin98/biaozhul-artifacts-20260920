# 实际运行记录

记录时间：2026-09-23。环境：Ubuntu（Linux 6.8）、OpenJDK 21.0.12、无外部依赖。

## 1. 环境

```text
$ java -version
openjdk version "21.0.12.1" 2026-08-18
OpenJDK 64-Bit Server VM (build 21.0.12.1+1-1-24.04.4-Ubuntu ...)
```

## 2. 构建命令与结果

```bash
$ ./build.sh
编译 31 个 Java 源文件...
构建完成：build/classes
```

退出码 0，无警告级以上问题。

## 3. 自动化测试命令与结果

```bash
$ ./run-tests.sh
[PASS] HandComputed          (33 项断言)
[PASS] Differential          (1 项断言)
[PASS] PolicyAndLateness     (13 项断言)
[PASS] ProcessingTime        (18 项断言)
[PASS] Json                  (23 项断言)
[PASS] Service               (21 项断言)
[PASS] HttpServer            (26 项断言)
--------------------------------------------------
全部测试通过：7 个测试类，135 项断言
$ echo $?
0
```

说明：`Differential` 显示为 1 项“场景计数”断言，但其内部实际跑了 **800 个
随机差分场景**（400 组随机序列 × 两种重叠策略），每个场景都会分别比较
“匹配对集合”和“未成功匹配的离场结局集合（TIMEOUT / INTERRUPTED_BY_C /
SKIPPED_AFTER_MATCH）”；任一场景不一致都会产生一条失败记录（开发过程中
该机制确实抓到过缺陷，见第 5 节）。

## 4. 服务实际运行（curl 冒烟）

`PORT=8080 WINDOW_MILLIS=100 ./run-server.sh` 启动成功，随后对全部样例
（`samples/*.json`）逐一请求，结果与 [`hand-computed.md`](hand-computed.md)
手算一致：

| 请求 | 实际结果 |
|------|----------|
| `01-multiple-a.json`（A1@0,A2@10,B3@60） | 2 个匹配 `(A1,B3) (A2,B3)`，两 A 保留 |
| `02-c-interrupts.json`（…C2@40…） | 0 匹配；`A1 INTERRUPTED_BY_C cId=C2` |
| `03-timeout.json`（A@0, B@120） | 0 匹配；`A1 TIMEOUT`，watermark=120 |
| `04-same-timestamp.json`（同刻 A,C,B） | 0 匹配；A1 被同刻但后到的 C 打断 |
| `05-boundary.json`（A@0, B@100） | 1 个匹配（闭区间右端成立） |
| `06-multi-key.json` | 仅 `(k1:A1,B3)`，k2 的 C 不影响 k1 |
| 先 A1@0,B2@80 再 `07-late-arrival.json`（A3@10） | 在线匹配 A1→B2；A3 进入 `lateDropped` |
| `08-replay-full-set.json`（完整集重放） | `consistent=true`，参考结果补回 `(A3,B2)`，共 2 个 |
| `POST /watermark {101}` | 存活的 A1 以 `TIMEOUT` 清理 |
| 改配置为 `SKIP_PAST_LAST` 后 A1,A2,B3 | 仅 `(A1,B3)`，A2 记 `SKIPPED_AFTER_MATCH` |
| 畸形 JSON / 非法类型 Z | HTTP 400 |
| 运行期 `POST /config {"mode":"PROCESSING_TIME"}` | HTTP 422 `MODE_IMMUTABLE` |
| `REJECT` 策略提交含迟到事件批次 | HTTP 422 `LATE_EVENTS_REJECTED [A3]`，同批 A4 未生效（随后 B5 只匹配 A1，验证原子性） |
| `MODE=PROCESSING_TIME` 启动 | 正常；事件自带 timestamp 被忽略、按墙钟匹配；PT 下 `/replay` 返回 422 |

以上响应均为服务真实返回；处理时间模式在 8099 端口冒烟通过（8081 当时被
机器上另一个无关服务占用，换到 8099 后正常——属环境问题，非本项目缺陷）。

## 5. 开发过程中出现过、并已修复的未通过项（如实记录）

最终状态为全绿，但开发过程中测试确实抓到过以下问题，均已修复并有回归覆盖：

1. **JSON 解析器接受非法数字 `01`（前导零）**
   最初的数字扫描过宽，`JsonTest` 的畸形输入用例失败。改为严格 JSON 数字文法
   （拒绝前导零、要求小数位/指数位完整）后通过。

2. **`/replay` 的参考结果始终为空数组（真缺陷）**
   构建 `referenceMatches` 时循环里只填了临时 map、忘记 `add` 进结果列表。
   `ServiceTest`/`HttpServerTest` 发现期望 2 个、实际 0 个；补 `refMaps.add(mm)` 后修复。

3. **重放排序的同刻 tie-break 用错了序号（真缺陷）**
   初版用引擎全局 `seq` 对重放事件排序，但乱序批次会让它与“服务接受顺序”
   不一致。改为以“服务接受顺序/请求数组下标”作为同刻次序后修复。

4. **参考实现把输入结束时仍存活的 A 误记为 TIMEOUT**
   在线流式语义里，输入末尾仍在窗口内的 A 是“仍在等待”，不是超时（需显式推进
   watermark 才超时）。差分测试（800 随机场景）报告多处这类结局不一致；删除参考
   实现末尾的“存活即超时”统计后，引擎与参考在全部 800 场景上一致。

5. **两处编译错误（已即时修复）**：`final` 字段 `deadline` 在重配窗口时需要重新
   赋值（改为非 final）；测试里一处 multi-catch 写成了有继承关系的两个类型
   （合并为 `RuntimeException`）。

6. **若干测试自身的断言问题（非产品缺陷）**：一个用例把预期状态码写成 400
   （实际语义为 422）；HTTP 测试用字符串 `contains` 匹配 pretty JSON 的
   `"key" : "value"`（冒号两边有空格）导致误报，改为解析 JSON 后按字段断言。

## 6. 已知边界 / 设计取舍

- 仅事件时间模式支持 `/replay`；处理时间语义依赖墙钟到达时刻，无法仅从历史
  timestamp 重建（接口对此显式返回 422）。
- 迟到事件无撤回（retraction）语义：`DROP` 不修改已发出的匹配；补算请走
  `/replay` 并显式提供完整事件集（在线被 DROP 的事件不进默认重放集）。
- 内存态服务：重启不保留事件与匹配（无持久化、无外部消息系统，符合需求边界）。
- 时间单位固定为 epoch 毫秒（`long`），加减做了饱和处理防止溢出。
