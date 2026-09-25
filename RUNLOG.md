# 运行记录（RUNLOG）

本文件如实记录开发/验收过程中的实际命令、结果，以及过程中出现并已修复的失败项。
环境：Ubuntu + **OpenJDK 21.0.12.1**；无 Maven/Gradle，无外部依赖。

## 1. 环境探测

```text
$ java -version    -> openjdk version "21.0.12.1"
$ javac -version   -> javac 21.0.12.1
$ mvn / gradle     -> command not found（故采用纯 javac + 自建脚本，零依赖）
$ 网络             -> 可达 Maven Central（但最终未使用任何外部库）
```

## 2. 构建与自动化测试

命令：

```bash
./build.sh test
# 等价：find src/main/java src/test/java -name '*.java' > build/sources.txt
#       javac -d build/classes @build/sources.txt
#       java -cp build/classes cep.test.AllTests
```

最终结果（干净重建后）：

```text
编译 34 个源文件...
编译完成 -> build/classes/
共 58 个测试：通过 58，失败 0，耗时 ~0.99 s
```

58 个测试覆盖：JSON（4）、手算短序列（17）、策略与边界（12）、全序/水位/调度器（8）、
乱序与迟到（7）、参考实现等价/随机差分（3）、HTTP 端到端（6）+ 收尾停服（1）。
完整 PASS 清单可复跑 `./build.sh test` 查看。

随机差分测试规模（固定种子、可复现）：
- 2000 轮：流式引擎 vs 独立参考实现，4 种策略 × 随机窗口/事件数/时间戳/同刻；
- 1000 轮：流式引擎 vs 独立 O(n²) 谓词定义（ALL_PAIRS）。

## 3. 服务与请求样例（实际运行）

```bash
./build.sh compile
java -cp build/classes cep.service.Main 0     # 端口0=系统分配，实测端口 40349
# 健康检查
curl -s --noproxy '*' http://127.0.0.1:40349/healthz
# -> {"status":"ok"}
```

> 注：本机 8080/8088/8090 等端口被其他程序占用（如另一个 `com.example.tjoin` 服务），
> 且环境设有 `HTTP_PROXY=127.0.0.1:7890`。故样例命令统一用 `--noproxy '*'` 直连，
> 并用端口 0 自动分配空闲端口；测试代码用 127.0.0.1 + 显式直连。

对 `samples/0*.json` 的 9 个 `POST /evaluate` 全部返回 **HTTP 200**，响应留档于
`samples/responses/*.response.json`。用独立 Python 脚本逐字段断言，结果：

```text
OK 01 matches=['A3->B1']            timeouts=['A4'] cKilled=2
OK 02 matches=['A1->B1','A2->B1','A3->B1'] timeouts=[] cKilled=0
OK 03 matches=['a->b']              timeouts=[] cKilled=0
OK 04 matches=[]                    timeouts=[] cKilled=1
OK 05 matches=['A1->B1']            timeouts=['A2'] cKilled=0
OK 06 matches=['A_late_ok->B1']     droppedLate=1
OK 07 matches=['A_late->B1'(late),'A2->B1'] timeouts=['A1'] acceptedLate=1
OK 08 agreesWithReference = True
OK 09 matches=['A1->B1','A5->B2']   timeouts=[]
```

有状态会话端到端（创建→追加→手动水位→快照→flush→409→replay）：

```text
追加 a1@0,a2@100,b1@400（wm 未推进）   -> matches now: 0
POST /watermark?watermark=401          -> matches: ['a1->b1','a2->b1']
POST /flush                            -> matches: ['a1->b1','a2->b1']（留档 10-*.json）
flush 后再 POST /events                -> HTTP 409 {"error":true,"status":409,...}
POST /replay                           -> replayed=true，matches 同上（留档 11-*.json）
归一化（replayed 标记、watermarks 操作计数）后首跑与重放结果完全一致: True
```

说明：首跑与重放仅两处**非结果**字段不同，均属预期——
`replayed`（重放响应特有）与 `stats.watermarks`（水位推进的“操作次数”：
重放只重放事件、不包含两次手动水位注入，故 5→4）。**匹配与超时集合逐字节一致**。

## 4. 过程中出现过的失败及处理（如实记录）

第一轮跑测试时 **57 个中 14 个失败**，全部定位并修复，没有跳过或删除失败用例：

1. **ALL_PAIRS 全配对测试期望错误**：测试最初假设一个 A 会被多个 B 重复匹配。
   明确语义为“一个 A 最多匹配一次；重叠指多个 A 共享同一 B”，并在 README/代码注释中写清，修正测试期望。
2. **迟到匹配的 `late` 标记缺失**：引擎只看了 `B.late`。修复为 `late = B.late || A.event.late`。
3. **随机差分两边喂入顺序不一致**：引擎按到达顺序（bound=0 会把乱序事件判迟到）、参考实现按排序。
   修正测试为两边都按全序 `(ts,seq)` 喂入，仅比较事件时间处理逻辑；乱序/迟到另有专项测试。
4. **独立 O(n²) 校验器与“C 杀 A/窗口过期”语义不符**：校验器最初是纯区间存在性枚举
   （未模拟 A 被 C 杀、被先前 B 消耗）。改为独立的按全序扫描（活 A 集合 + C 清除 + B 窗口配对后移除 A）。
5. **调度器测试自相矛盾的断言**：先断言 `advanceTime(50)` 触发 deadline=50，又断言不触发。
   统一为半开语义 `deadline < t`（即 advanceTime(50) 不触发 50，51 触发），与引擎“事件先于同刻定时器”一致。
6. **HTTP 测试 ConnectException（5 个）**，三个叠加原因，逐一排除：
   - 环境 `HTTP_PROXY` 把到 localhost 的请求发往代理 → 客户端显式直连；
   - `localhost` 解析到 IPv6 `::1` 而服务绑 IPv4 → 改用 `127.0.0.1`；
   - **真正根因**：测试在 `register()` 里用 `finally { server.stop(); }`，注册方法返回时
     （测试体尚未执行）服务就被停掉 → 改为最后一个测试项负责停服。
   - 期间还踩到 JDK HttpClient 的坑：自定义 ProxySelector 返回**空列表**会让客户端挂起，
     必须返回 `List.of(Proxy.NO_PROXY)`。
7. **会话测试时序**：水位语义下 `wm=400` 不处理 ts=400 的事件（需 wm>ts），
   最初在追加后立刻断言匹配数=2 失败。改为先 `watermark=401` 再断言（也正好演示手动水位 API）。
8. **JSON 数字**：flush 的终局水位 `Long.MAX_VALUE` 被写成科学计数法 `9.22..E18`。
   修改 `JsonWriter`：凡 long 范围内的整数一律按整数原样输出。

修复后第二轮 58/58 全部通过，未再出现失败；干净重建（`rm -rf build && ./build.sh test`）复跑同样 58/58。

## 5. 如何复现

```bash
./build.sh test                                   # 58 个自动化测试
java -cp build/classes cep.service.Main 0         # 起服务（0=自动端口，看启动日志端口）
./samples/curl-demo.sh http://127.0.0.1:<port>    # 9 个样例 + 会话/重放
```
