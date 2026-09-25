# 运行记录（如实）

环境：Ubuntu（Linux 6.8），OpenJDK 21.0.12；无 Maven/Gradle/第三方库。
以下命令与输出均为实际执行所得；时间戳：2026-09-23。

## 1. 工具链

```
$ java -version
openjdk version "21.0.12.1" 2026-08-18
$ javac -version
javac 21.0.12.1
$ mvn -version / gradle -version
command not found（故采用 javac 直连 + 自写测试框架，保证离线可构建）
```

## 2. 编译

```
$ ./build.sh
main sources compiled -> build/
test sources compiled -> build/
```
（javac 对泛型强转有 unchecked 提示，非错误，未改动。）

## 3. 自动化测试

```
$ ./test.sh
...
tests: 37, passed: 37, failed: 0
```

测试分布：定时器 8、核心算子 13、离线对照 4（含 600 组随机乱序）、
JSON 7、服务（含真实 HTTP 端到端）5。

### 开发过程中实际出现并修复的问题（不回避）

首跑为 35 项中 12 项失败，暴露的都是真实缺陷，修复后全部通过：

1. **封窗/合并边界不一致**：最初定时器用严格 `W > end+gap` 触发，而合并是
   `间隔 <= gap`（闭区间），导致 `W == end+gap` 时本可合并的事件被抢先封存。
   统一为闭区间。
2. **窗口-窗口相连判定写错**：`Window.connects(Window,gap)` 初版条件方向反了，
   桥接事件同时选中两窗后，传递闭包阶段把实际相连的窗口判为不相连（一个迟到
   桥接只并了一个窗口）。改为 `max(start) - min(end) <= gap`。
3. **清除定时器误用闭区间**：状态清理若在 `W == end+gap+L` 触发，会在"允许
   迟到的最后一个有效时刻"提前删状态。新增 `registerTimerAfter`（严格 `W > t`）
   专用于清理，封窗仍用闭区间。
4. **处理事件后同值重放水位线**：曾用一次同值 `advanceWatermark` 触发即时封窗，
   会误触发严格清除定时器。改为合并后直接封存，不做重放。
5. JSON 解析器曾接受非法的前导零数字（如 `01`），已按 JSON 规范拒绝。
6. 测试侧修复：`Integer` 与 `Long` 装箱比较为假（断言库改为按数值递归比较）；
   两处测试剧本的时间数学写错（桥接事件实际间隔大于 gap、水位线推进过早导致
   窗口先被清除），已重新精确设计。

未通过项：当前 **0 项**。

## 4. 离线样例运行

```
$ ./run-samples.sh
ran samples/01-bridge-unordered.json -> samples/out/01-bridge-unordered.response.json
ran samples/02-gap-equality.json      -> samples/out/02-gap-equality.response.json
ran samples/03-late-after-seal.json   -> samples/out/03-late-after-seal.response.json
ran samples/04-multi-key-bounded.json -> samples/out/04-multi-key-bounded.response.json
```

关键结果（完整 JSON 见 `samples/out/`）：

- `01-bridge-unordered`：changelog 为
  `ADD[1,5]=20, ADD[21,25]=200, RETRACT[1,5]=20, RETRACT[21,25]=200, ADD[1,25]=1220`；
  W=56 后 `activeWindowsRetained=0, sealedWindowsRetained=0`（状态已清理）。
- `02-gap-equality`：`eq`（间隔==gap）合并为 `[0,20],count=3`；
  `split`（间隔 gap+1）拆为 `[0,0]`、`[11,11]`；丢弃 0。
- `03-late-after-seal`：received=3，droppedLateEvents=2；最终结果保持
  `[0,0],count=1`（迟到事件既未改结果也未留状态）。
- `04-multi-key-bounded`：7 收 1 丢；user-1 两窗、user-2 两窗，跨键隔离。

## 5. 独立语言交叉校验

除 Java 内置的离线参考实现外，另用 Python 独立实现离线分组与门控，
对四个样例做独立比对（`crosscheck.py`）：

```
$ python3 crosscheck.py
01-bridge-unordered.json: windows OK; received=5 drops=0 stats OK
02-gap-equality.json: windows OK; received=5 drops=0 stats OK
03-late-after-seal.json: windows OK; received=3 drops=2 stats OK
04-multi-key-bounded.json: windows OK; received=7 drops=1 stats OK
exit=0
```

## 6. HTTP 服务实测

```
$ java -cp build sessions.Main serve 18099
event-time session-window service listening on port 18099

$ curl -s http://localhost:18099/health
{ "status": "ok" }

$ curl -s -X POST localhost:18099/sessions/run -d @samples/01-bridge-unordered.json
finalResults: [{"key":"a","start":1,"end":25,"aggregate":1220}]
changelog: ADD[1,5]=20, ADD[21,25]=200, RETRACT[1,5]=20, RETRACT[21,25]=200, ADD[1,25]=1220

$ curl ... -d 'not json'                 -> HTTP 400 invalid JSON
$ curl ... -d '{"gap":1}'               -> HTTP 400 'input' must be an array
$ curl -X GET .../sessions/run          -> HTTP 405
```

注：本机 8099/8080 被其他服务占用（首次用 8099 启动时 `BindException`），
改用 18099 成功；`serve 0` 也可由系统分配空闲端口。
