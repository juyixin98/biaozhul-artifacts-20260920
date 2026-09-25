# 运行记录（如实）

本机与工具链：

```
OS      : Linux 6.8.0-90-generic (x86_64, Ubuntu 24.04)
JDK     : OpenJDK 21.0.12.1（javac 21.0.12.1）
构建工具: 无 Maven/Gradle；仅用 JDK 自带 javac/java
依赖    : 零第三方库（HTTP 用 JDK 内置 com.sun.net.httpserver，JSON 手写）
curl    : 8.5.0
```

## 1. 编译

命令：

```bash
./build.sh
```

最终结果（完整输出见 `docs/build-output.log`）：

```
>> 编译 35 个 Java 源文件 -> build/classes
>> 构建完成
BUILD_EXIT=0
```

过程中出现并已修复的编译问题：

1. 三个源文件包名误写为 `drbv.*`（应为 `drvb.*`），导致 `cannot find symbol`；
   统一更正后通过。
2. 三个 HTTP handler 方法内调用了会抛 `IOException` 的响应方法但未声明
   `throws IOException`；补齐声明。
3. `RuleVersionTable.markReclaimed` 原为包级可见，测试包无法访问；改为
   `public` 并在 Javadoc 中注明"业务代码应走带前提检查的
   `ReclamationService.reclaim`，本方法仅供回收服务与测试直接构造墓碑"。

## 2. 自动化测试

命令：

```bash
java -cp build/classes drvb.tests.TestRunner build/classes
```

最终结果（完整输出见 `docs/test-output.log`）：

```
测试摘要: 29/29 通过，0 失败
TEST_EXIT=0
```

29 个用例分布：JSON 4、时间/调度 3、谓词 5、版本绑定表 5、回收前提 5、
端到端验收场景 5、真实 HTTP 回环 2。

首轮并非全绿，**曾有 4 个失败 + 1 批 SKIP，均已定位并修复**：

| 现象 | 根因判定 | 处理 |
| --- | --- | --- |
| 全部用例 `[SKIP] 非法签名` | 自研运行器只接受 `static` 方法，而测试是实例方法 | 改运行器：每类实例化一次，实例/静态方法均支持 |
| `in 缺 values` 抛出的是 `JsonException` 而非 `RuleException` | **真实代码缺陷**：发布期规则校验未把 JSON 类型错误归一成规则错误 | 修 `Predicates`：`values`/`predicates` 类型不符显式抛 `RuleException`（非法规约在发布期 400，而非 500） |
| `200 命中的是第 2 条绑定` 预期 1 | 测试断言写错：200 本就应命中第 2 条绑定 | 更正断言为 2 |
| health/runCount 的 `contains` 失配 | 测试字符串按 `"k" : v` 匹配，pretty 输出实为 `"k": v`（冒号前无空格） | 更正测试匹配串 |
| 单事件响应取顶层 `status` 得到 null | 测试取错层级：单事件结果在 `result` 字段内（批量在 `results[]`） | 更正测试读取 `result` |

除上表第 2 行是产品代码缺陷外，其余均为测试自身错误，已一并改正。

## 3. 端到端演示（真实启动服务 + curl）

命令：

```bash
./demo.sh            # 默认 --port=0，由系统分配空闲端口
```

结果（完整输出见 `docs/demo-output.log`）：退出码 0，日志中 0 处
`404 page not found`；13 个步骤的 JSON 响应均符合预期。其中可直接核对的关键
观测：

- 乱序：先收 `eventTime=2500`（命中 v2，FILTERED_OUT），再到晚到
  `eventTime=500`（`late=true`，仍用历史 **v1**，MATCHED）。
- 边界：`eventTime=1000` 归 v2（`bindingSeq=2`）；回滚后 `eventTime=3000`
  归 v1（`bindingSeq=3`），证实左闭右开。
- 缺失版本：`eventTime=-1` → `REJECTED / MISSING_RULE_VERSION`，未套用最新规则。
- 回滚：新增 `seq=3, operation=ROLLBACK` 绑定，旧两条绑定区间在 `/state` 中
  原样保留（effectiveTo 分别为 1000、3000）。
- 回收前提：闸门 2900 < 区间结束 3000 时 `eligible=false`；闸门到 3000 时
  `eligible=true`（边界 `≤` 判定）。
- 回收前后对比：同一 v2 历史区间，回收前晚到事件 MATCHED(v2)；执行
  `mode=eligible` 仅回收 v2 后，更晚到达的同区间事件变为
  `REJECTED / RECLAIMED_RULE_VERSION`，而非改判 v1；当前区间事件仍正常走 v1。
- 可注入时间：`/admin/tick {"advanceBy":5000}` 把处理时间 90000 → 95000。

### 首次运行 demo 的环境问题（如实记录）

首次执行时显式指定了端口 18080，而该端口已被本机另一个进程
（`vccsim`，pid 517275）占用：我们的服务抛
`java.net.BindException: Address already in use` 退出，就绪探测却连上了
占位服务，导致全部请求得到纯文本 `404 page not found`。

修复：demo 默认改为 `--port=0` 由操作系统分配空闲端口，从启动日志解析实际
端口，并把健康检查从"端口能连上"收紧为"返回体含 `\"status\": \"UP\"`"，
避免再次误连到其他服务。修复后重跑通过。

## 4. wall 时钟模式抽查

除自动化测试覆盖的 manual 确定性路径外，另以 `--clock=wall
--auto-reclaim-ms=500` 真实启动验证：

- `/health` 返回 `"clockMode": "wall"`，`processingTime` 为真实系统毫秒时间戳；
- 用 `curl -d @examples/...json` 发布规则、提交事件均成功（201/200）；
- `/admin/scheduler` 能看到真实调度线程上的 `auto-reclaim-versions` 任务，
  等待约 1.3s 后 `runCount=2`。

复测中发现并修复的一个可观测性缺口：最初 `/admin/scheduler` 只在 manual
模式列出任务，executor 模式恒返回空数组。已在 `Scheduler` 接口增加
`tasks()`，两种实现都暴露任务句柄，复测通过。

## 5. 未通过项 / 已知限制

- 最终状态：编译 0 错误，**自动化测试 29/29 通过，demo 退出码 0**，无遗留
  未通过项。
- 已知范围限制（设计如此，非缺陷）：纯内存、无持久化，重启状态清空；事件经
  HTTP 同步提交，不接消息中间件；谓词为声明式算子而非通用脚本；不含前端。
  详见 `docs/DESIGN.md` 第 8 节。
