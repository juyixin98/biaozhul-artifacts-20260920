# 实测记录（RESULTS）

记录时间：2026-09-23（Asia/Shanghai）。以下均为在本机实际执行的结果，未做修饰。

## 环境

- 机器初始**没有安装 Java**，且当前用户无 sudo（容器禁用提权），因此无法 apt 安装。
  实际做法：下载 Temurin 免安装 JDK 解压到 `~/jdks/`。
  - 发行版：Temurin OpenJDK **17.0.20.1+1**（`openjdk version "17.0.20.1" 2026-08-18`）
  - 下载地址：`https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jdk/hotspot/normal/eclipse`
  - 安装包 SHA-256：`3808d1d15e3ec6bd5b84057fb5d84c33d8a1536a258146bcea2e603fc726e08e`
- 编译：`javac -Xlint:all`，**主代码与测试均零 warning、零 error**。
- 依赖：编译/测试/运行全程未访问任何 Maven 仓库，无第三方 jar。

## 自动化测试

命令：`./test.sh`（内部：`build.sh` 编译主代码 → `javac` 编译测试 → `java` 运行 main 测试类）。

结果（最后两行）：

```
--------------------------------------------------
passed: 63, failed: 0
```

退出码 `0`。63 条断言全部通过，覆盖：

- 全量遍历 23 行不重不漏；`score=50` 五行按 `a-005…a-009`（id 升序）稳定输出；
- `limit` = 1 / 2 / 7 / 23 / 100 五种页大小遍历序列完全相同；
- `order=desc` 时序列与独立计算的 `(score desc, id asc)` 期望完全一致，重复键仍按 id 升序；
- 遍历拿到第 1 页后：插入 3 行（score 15/50/999）、删除 2 行未翻到的行
  （`a-010`、`a-020`）、把 `a-009` 的 score 50→51（未见行）、`a-001` 10→11（已见行），
  继续翻页：拼回恰好原 23 行、无重复、快照 id 不变、快照内 `a-009.score` 仍是 50；
  随后另起首页能看到全部新变更，快照版本号增大；
- 筛选遍历（category=music，limit=2）中途插入同类目行，旧遍历仍是 8 行、新行不可见；
  `nameContains=alp` 大小写不敏感只命中 Alpha；
- 改 category / 加 nameContains / 换 sort / 换 order / 改 limit / 无筛选游标用于筛选查询，
  6 种场景均 `400 QUERY_MISMATCH`；
- 游标伪造 9 种：垃圾串、错版本前缀、截断、非法 Base64、翻转 payload 字节、翻转 tag 字节、
  异密钥签名 → `400 CURSOR_INVALID`；合法签名+不存在快照 → `410 SNAPSHOT_EXPIRED`；
  合法签名+版本号篡改 → `400 CURSOR_INVALID`；
- 800ms TTL：1100ms 后续用游标 → `410 SNAPSHOT_EXPIRED`；TTL 内续用 200；
- CRUD/校验：201、重复 id 409、PATCH 200 且 GET 可见、404、删除后再删 404、
  缺字段 MISSING_FIELD、score 传字符串 INVALID_FIELD、坏 JSON BAD_JSON、
  limit 0 / 101、非法 sort/order、未知路径 404、PUT → 405；
- `CursorCodec` 单元：正常往返、异密钥拒绝、垃圾串拒绝。

## 手工冒烟（真实 HTTP）

1. `PORT=8080 PAGER_MAC_SECRET=demo-fixed-secret ./run.sh` 启动：
   `stable-pager listening on http://localhost:8080`，`/healthz` → `{"status":"ok"}`。
2. `./examples.sh http://localhost:8080` 退出码 0，实测现象：
   - 首页 5 行顺序 `a-021(5), a-001(10), a-002(20), a-013(20), a-003(30)`
     ——两个 score=20 按 id（a-002 在 a-013 前）稳定排列；第 2 页开头即 `a-004…`，
     两页快照 id 相同、版本同为 23；
   - 「分页中插入 demo-x(33)、a-009 改 51、删 a-020」后用旧游标取后续 10 行：
     输出为旧快照的 `a-009(score=50), a-010, a-011, … a-019`，**无 demo-x、无重漏**，
     快照版本仍 23；
   - 带 `category=books` 复用无筛选游标：
     `HTTP 400 {"error":"QUERY_MISMATCH", … "cursorQuery":"…limit=10","requestQuery":"…category=books…limit=5"}`；
   - 游标内 `x→y` 翻转一个字符：
     `HTTP 400 {"error":"CURSOR_INVALID","message":"invalid cursor: cursor signature does not match"}`；
   - `category=music&sort=name&order=desc&limit=3` 返回 Whiskey/Tango/Quebec。
3. 独立核对：用 limit=7 手工翻完整个结果集 → 4 页、合计 23 个 id、去重后仍 23 个；
   `limit=100` 单页 `hasMore=false`、23 行。
4. 另起 `PAGER_SNAPSHOT_TTL_MS=2000` 的实例（8090 端口，因沙箱无法 kill 上一进程，
   8080 仍被占用，详见下节）：TTL 内续页 `HTTP 200`；sleep 3s 后续用同一游标：
   ```
   HTTP 410
   {"error":"SNAPSHOT_EXPIRED","message":"snapshot '8085f443…' has expired or does not exist; restart the walk from the first page"}
   ```

## 已知问题 / 未完成项（如实列出）

1. **进程清理受沙箱限制**：手工演示启动的两个 Java 服务（8080 端口的 60s-TTL 实例、
   8090 端口的 2s-TTL 实例）因运行在沙箱 PID 命名空间之外，`kill`/`pkill` 在本会话内
   返回 `No such process` 而端口仍处于监听（环境/容器回收时会随之结束；如需在当前机器
   立即释放，需要在宿主侧 `kill` 对应 java 进程，或更换端口启动）。不影响代码与测试结论。
2. **无持久化**：数据全在内存，重启回到 23 行种子集；这是题目「纯后端内存服务」范围内
   的有意取舍，未实现存储层。
3. **未设置鉴权/限流/TLS**：接口在内网/本地直接暴露，生产部署需在前置网关补齐。
4. **未提供 JUnit 版本测试**：为保证「锁定零依赖、完全离线可跑」，测试是一个自带
   断言与 main 的普通 Java 类（`./test.sh` 运行），没有引入 JUnit/Surefire。
5. 快照为整表防御拷贝（简单可靠）；超大数据集应改为增量多版本存储，本次未实现。
