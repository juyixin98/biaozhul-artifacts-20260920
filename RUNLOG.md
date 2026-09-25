# 运行记录（RUNLOG）

记录本项目的实际构建、测试与服务运行过程，含失败项与修正。所有命令在
`/home/admin/Downloads/biaozhul/opp122/b` 下执行。

## 环境

```
$ java -version
openjdk version "21.0.12.1" 2026-08-18
$ javac -version
javac 21.0.12
```

无 Maven/Gradle，构建只用 JDK 自带 `javac`（见 `scripts/build.sh`），
无任何第三方依赖、无网络访问。

## 1. 构建

```
$ bash scripts/build.sh
Build OK -> build/classes
```

结果：**通过**（`javac -encoding UTF-8 -Xlint:all`，无警告失败）。
完整输出：`runlogs/01-build.log`。

## 2. 自动化测试

```
$ bash scripts/test.sh
```

最终结果：**40/40 通过，0 失败**（`runlogs/02-test.log`）：

```
Tests run: 40, passed: 40, failed: 0
```

### 失败与修正过程（如实记录）

第一轮运行（开发中）出现 **6 个失败**，全部定位为**测试用例中手工计算的期望值错误**，
生产实现与蛮力穷举参考（`BruteForceReference`）始终互相一致：

| # | 失败用例 | 原因 | 修正 |
|---|---|---|---|
| 1 | `matcher/aba-exhaustive-enumeration` | 手算把 `a b a b a` 上 slop1 的合法元组误写为 4 个；实际差 3 的元组（如 (1,2,5)）在 slop1 下不合法，正确为 2 个（slop2 才是 4 个） | 改为断言 slop1=2、slop2=4 |
| 2 | `matcher/result-limit-truncates` | 基线匹配数漏算 `(3,5)`，应为 3 对而非 2 对 | 基线改为 3 |
| 3 | `search/d1-repeated-echo-enumerates-all-pairs` | d3 的 `rain rain` slop2 漏算 body 内 `(4,7)`（差 3 合法），应为 2 对 | 改为断言 2 对并逐对校验 |
| 4 | `search/stopword-keeps-position-cat-mat-needs-slop` | 位置差 4 对应 slop=3（差-1），误写为需要 slop4 | 改为 slop0..2 不命中、slop3 命中 |
| 5 | `api/search-stopword-analyzer` | 同 #4 | 同步修正 |
| 6 | `json/roundtrip-nested` | 期望值用 `Integer`，解析结果为 `Long`，`Integer(2) != Long(2)` | 期望值改用 `2L` 等 |

第二轮运行剩 **1 个失败**：`cat sat mat` slop2 实际有 **2 条**命中
（跨字段 `(2,5,8)` 与 body 内 `(4,5,8)`），断言只取了第一条。修正为穷举两条后，
第三轮起 40/40 全部通过。

说明：这些失败均为测试期望的手算错误，且正是被“蛮力穷举参考 + 穷举枚举”
的验收手段暴露出来的——生产匹配器与蛮力参考的等价性测试（300 组随机小序列 +
全语料组合）自始至终通过。

## 3. CLI 冒烟

```
$ java -cp build/classes phrase.Main search "echo echo" --slop 0
  → d1: positions=[4,5],[5,6],[6,7]（3 对相邻 echo，全部在 body）
$ java -cp build/classes phrase.Main search "data pipeline" --slop 0
  → d2: positions=[8,9] fields=[body,tags] [cross-field]
$ java -cp build/classes phrase.Main search "cat mat" --analyzer stopword --slop 2
  → 0 命中（停用词保留位置，cat@4 mat@8 差 4）
$ java -cp build/classes phrase.Main search "cat mat" --analyzer stopword --slop 3
  → d5: positions=[4,8]
$ java -cp build/classes phrase.Main search "blue ocean" --slop 0 --field-gap 1
  → 0 命中（字段间 1 个虚拟位置，差 2）
$ java -cp build/classes phrase.Main search "blue ocean" --slop 1 --field-gap 1
  → d7: positions=[2,4] [cross-field]
$ java -cp build/classes phrase.Main search "data pipeline" --field body
  → 0 命中（pipeline@9 在 tags 字段，被字段限制排除）
```

结果：**全部符合预期**。

## 4. HTTP 服务实测

```
$ java -cp build/classes phrase.Main serve --port 18231
phrase-search service listening on http://127.0.0.1:18231
```

用 `bash examples/curl-examples.sh 18231` 发送 `examples/requests/` 下全部样例，
响应与 `examples/responses/` 中保存的一致（`runlogs/04-curl-examples.log`）：

- `POST /analyze`：`the cat sat in the mat` → `cat@2, sat@3, mat@6`（停用词位置保留）；
- `POST /search` `echo echo` slop0 → d1 三条 `(4,5)(5,6)(6,7)`；
- `POST /search` `data pipeline` slop0 → d2 `(8,9)`，`crossField: true`，`fields: [body, tags]`；
- `POST /search` `cat mat` slop3 stopword → d5 `(4,8)`；
- `POST /search` terms 数组 `["alpha","alpha","beta"]` → d4 `(2,3,4)`；
- `POST /search` `data pipeline` field=body → 0 命中。

错误处理实测（`runlogs/05-curl-errors.log`）：

| 请求 | 结果 |
|---|---|
| `slop:-1` | HTTP 400 `slop must be between 0 and 1000` |
| stopword 模式查询 `the a an` | HTTP 400 `query produced no terms after analysis (all terms are stopwords?)` |
| `GET /nope` | HTTP 404 `not found: /nope` |

### 运行中的小插曲（如实记录）

- 首次以 `--port 18080` 启动失败：`BindException: Address already in use`（端口被本机其他进程占用），改用 18231。
- 记录运行日志时曾把旧服务进程遗留，第二次启动同端口再次 `BindException`；
  用 `pgrep -f 'phrase.Main serve' | xargs kill` 清理后重启成功，随后正常 `kill` 停止。

## 5. 未通过项 / 遗留问题

- 最终状态：**无未通过测试**（40/40），无已知功能缺陷。
- 已知限制（设计内，非缺陷）：内存索引、进程退出即失；停用词表固定；
  每文档匹配数默认上限 100（超出返回 `truncated: true`）；仅监听 127.0.0.1。
