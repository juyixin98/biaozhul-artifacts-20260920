# 运行记录（RUNLOG）

- 日期：2026-09-24
- 机器：Linux 6.8.0-90-generic x86_64（Ubuntu 24.04）
- JDK：`OpenJDK 21.0.12.1`（`java -version` / `javac -version` 均为 21.0.12.1）
- 依赖：**零第三方库**（JSON 与 HTTP 均用 JDK 自带能力实现），全程离线，未调用外部搜索服务或大模型。

## 1. 编译

命令：

```bash
rm -rf build
./build.sh
```

结果（退出码 0）：

```
compiled main sources -> build/classes
```

## 2. 自动化测试

命令：`./test.sh`（先编译主代码与测试，再执行 `com.example.edcand.AllTests`）
结果：**退出码 0，22 个测试全部通过，0 失败**：

```
PASS  lev: known ascii pairs
PASS  lev: both empty
PASS  lev: unicode code points (not UTF-16 units)
PASS  lev: codepoint distance must not equal UTF-8 byte distance
PASS  lev: cappedDistance agrees with full DP (random fuzz)
PASS  lev: cappedDistance fuzz with emoji code points
    (ascii rounds: 7200 query/threshold combos)
PASS  recall: random small ASCII vocab vs brute-force (zero miss/false-positive)
    (unicode rounds: 3000 query/threshold combos)
PASS  recall: random Unicode vocab (emoji/combining/CJK) vs brute-force
PASS  recall: empty string query and empty term
PASS  recall: combining chars under NFC vs NFD
PASS  recall: long common prefix family vs brute-force

PASS  recall: threshold 0 is exact match only
PASS  recall: full synthetic corpus vs brute-force (sample queries)
PASS  norm: NFC merges NFD combining sequence
PASS  norm: NFD splits into base + combining mark
PASS  norm: NFKC folds full-width and ligatures
PASS  norm: case folding applied in all modes except NONE
PASS  norm: emoji unchanged by normalization, still 1 code point
PASS  norm: parse rejects unknown form
PASS  json: round-trip object with unicode and emoji
PASS  json: parses \u escape and rejects bad input
PASS  server: health/stats/distance/search over HTTP, incl. recall

22 tests, 22 passed, 0 failed (1050.8 ms)
```

验收对拍规模：随机 **ASCII** 小词表 300 轮 × 6 查询 × 4 阈值 = **7200** 组；
随机 **Unicode**（码点池含 emoji U+1F600/U+1F601、组合重音 U+0301、CJK）
200 轮 × 5 查询 × 3 阈值 = **3000** 组。每组均与全扫描完整 DP 结果逐项比对，
结论：零漏报、零误报、距离值完全一致。另有阈值受限 DP 对完整 DP 的随机对拍
4000 组 ASCII + 2000 组 emoji 码点池。

## 3. CLI 演示与码点距离

```
$ java -cp build/classes com.example.edcand.App demo
corpus terms: 571 (q=2, NFC + case fold)

query="" (codepoints=0) k=1 -> 11 match(es); ... 含空串自身 d=0
query="peple" k=1 -> 2 match(es); lengthGate=329 candidates=2 exactCalls=2
    d=1  "eple"
    d=1  "people"
query="café"(NFD 输入) k=1 -> 1 match(es); candidates=1
    d=0  "café"
query="document_section_paragraph_twon" (codepoints=31) k=2 -> 4 match(es); lengthGate=10 candidates=7
query="😀" (codepoints=1) k=1 -> 11 match(es)   # emoji 按 1 码点计
query="日本語" k=1 -> 1 match(es) d=0
NFKC 对照："ａｂｃ" 在 NFKC 下命中 "abc" d=0，NFC 下不命中

$ java -cp build/classes com.example.edcand.App dist e é NONE
distance=1  codePoints(a)=1 codePoints(b)=1  norm=NONE
```

## 4. HTTP 服务实测（samples/ 中的请求样例）

启动：`java -cp build/classes com.example.edcand.App serve 8080`

```
### GET /health
{"status":"ok"}
### GET /stats
{"totalTerms":571,"q":2,"normalization":"NFC","unit":"unicode_codepoint"}
### POST /distance < samples/distance-nfd-nfc.json   (NFD "café" vs NFC "café")
{"distance":0,"codePointLengths":[4,4],"normalizedA":"café","normalizedB":"café","normalization":"NFC"}
### POST /distance < samples/distance-emoji.json
{"distance":1,"codePointLengths":[1,1],"normalizedA":"a","normalizedB":"😀","normalization":"NONE"}
### POST /search < samples/search-typo.json
{"matches":[{"term":"development","distance":1},{"term":"devzelopment","distance":2}],
 "totalTerms":571,"passedLengthGate":39,"candidates":2,"exactLevenshteinCalls":2}
### POST /search < samples/search-long-prefix.json
{"matches":["...ton" d=1,"...two" d=1,"...ten" d=2,"...tone" d=2],
 "passedLengthGate":10,"candidates":7,"exactLevenshteinCalls":7}
### POST /search < samples/search-nfkc-fullwidth.json
{"matches":[{"term":"abc","original":"ａｂｃ","distance":0},{"term":"arc","distance":1},{"term":"rbc","distance":1}]}
### POST /search < samples/search-empty.json
空查询 k=1：命中空串 d=0 及 10 个长度为 1 的词项 d=1（含 "😀"、"🎉"、"é"、"ⅲ" 等）

### 错误请求
非法 JSON -> HTTP 400；threshold=-1 -> HTTP 400；
缺字段 -> 400；normalization=WAT -> 400（端到端测试中覆盖并通过）
```

## 5. 字节距离 ≠ 字符距离的实测对照

测试 `lev: codepoint distance must not equal UTF-8 byte distance` 在同一次运行中
计算两种距离并断言不同：

| 字符串对 | Unicode 码点 Levenshtein | UTF-8 字节序列 Levenshtein |
|---|---|---|
| `e` vs `é`(U+00E9) | **1** | **2**（字节 65 → C3，再插入 A9） |
| `a` vs `😀`(U+1F600) | **1** | **4**（emoji 的 UTF-8 为 F0 9F 98 80） |

实现中没有任何 `getBytes` 参与距离计算；仅在测试里用字节 DP 作为对照证据。

## 6. 过程中出现过的失败与修复（如实记录）

首次运行测试时 22 个中 4 个失败，均已修复并在随后的干净构建中全部通过：

1. **JSON 整数被解析成 Double（2 处）+ 连带 E2E 1 处**：
   自写 JSON 的 `number()` 用三元表达式 `cond ? Double.valueOf : Long.valueOf`，
   数值条件表达式发生二进制提升，两个分支被统一成 `double`，`42` 解析为 `42.0`。
   改为 `if/else` 中赋值给 `Object` 局部变量，保持 `Long` 原类型。
2. **`é`(NFC) 与 `e+U+0301`(NFD) 的原始码点距离断言错误**：
   初版测试期望 1，实际正确码点距离为 **2**（替换 é→e 再插入组合重音符）。
   已修正测试期望为 2（规范化为 NFC 后距离才为 0，另由专门测试覆盖）。
3. **JSON 往返测试 NPE**：`List.of(..., null, ...)` 不允许 null 元素，
   改为 `Arrays.asList`。

另外在代码评审过程中清理了两处开发残留：`cappedDistance` 中一行无意义的中间语句、
`CandidateIndex` 中一个未使用变量，以及测试文件中误写的占位 `Map` 接口。

## 7. 未通过项 / 已知边界

- 最终状态：**无未通过项**。22/22 测试通过，服务手工实测全部符合预期。
- 环境备注：验证机的 8081 端口被本机无关进程 `mpserver` 占用（`BindException:
  Address already in use`），非本项目问题；最终冒烟测试改用操作系统临时分配的
  空闲端口（当次为 47577）验证通过。`serve 0` 即支持自动选择端口。
- 距离单位刻意选为 Unicode **码点**而非字素簇（国旗、ZWJ 家庭 emoji、组合序列
  按“感知字符”计数会不同）。README 第 5 节说明了需要字素簇时的接入点
  （`BreakIterator.getCharacterInstance()`），当前不默认分簇。
- 服务仅监听 127.0.0.1，无鉴权/持久化，定位为本地库与演示服务。
