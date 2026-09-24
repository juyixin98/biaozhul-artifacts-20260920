# 实际运行记录

记录时间：2026-09-24
机器：Linux 6.8.0-90-generic x86_64（Ubuntu 24.04）
JDK：OpenJDK 17.0.20.1（`/usr/bin/java`），无任何第三方依赖。

## 1. 自动化测试

命令：

```bash
./test.sh
```

结果（退出码 0，原文摘录）：

```
── Bitmap 基础运算
── RLE 随机往返（压缩不改变行 ID）
── RLE 边界模式
── CSV 解析
── 空全集（0 行数据集）
── 高基数列与索引空间统计
    city 低基数(成块聚集): raw=2528 B, rle=24 B (0.95%)
    cityCyclic 低基数(严格交替): raw=2528 B, rle=10006 B (395.81%)
    sku  高基数(5000 个单点位图): raw=3160000 B, rle=29742 B (0.94%)
    flag 交替最坏游程: raw=256 B, rle=2002 B (782.0% —— 压缩率差是真实统计)
── 删除后的 NOT 取补语义
── 删除/压缩后行 ID 不变化
── 随机布尔表达式 vs 逐行扫描（4 个存活阶段 × 600 表达式）
    阶段1 无删除: alive=400/400, 600 条随机表达式全部匹配
    阶段2 按 ID 删除 1/4: alive=300/400, 600 条随机表达式全部匹配
    阶段3 再按表达式删除: alive=260/400, 600 条随机表达式全部匹配
    阶段4 部分恢复: alive=270/400, 600 条随机表达式全部匹配
── HTTP 端到端（真实 HttpServer + HTTP 回环）
    stats 摘要: rowCount=5 indexRawBytes=40 indexRleBytes=22 ratio=0.55

================================================
全部测试通过 ✅  断言数: 232913
```

核心验收点：**400 行（含 400 个唯一值的高基数列 sku）随机数据，4 个存活阶段
（无删除 / 按 ID 删 1/4 / 再按表达式删 / 部分恢复）各 600 条递归随机布尔表达式
（eq/in/and/or/not/alive，深度 ≤3），共 2400 条与逐行扫描参考实现逐行 ID
比对，0 条不一致。** 此外 RLE 编解码对 14 种长度 ×40 轮随机位图往返校验，
行 ID 数组完全一致。

## 2. HTTP 示例（对 data/sample.csv 实际执行）

启动：

```bash
java -cp out bitserver.Main 8080 --autoload "$PWD/data/sample.csv"
# 已自动加载数据集: .../data/sample.csv
# 位图索引服务已启动: http://localhost:8080/
```

实际响应（节选，均为真实执行所得）：

```
GET  /health
  -> {"status":"ready","loaded":true}

POST /query  city=BJ
  -> {"ids":[0,1,2,12,16],"count":5,"matchedAlive":5,"aliveCount":20,"totalCount":20}

POST /query  city in {BJ,SZ}
  -> {"ids":[0,1,2,9,10,11,12,15,16,19],"count":10,...}

POST /query  OR(AND(city=BJ,active=true), grade=A)
  -> {"ids":[0,2,5,8,13,15,18],"count":7,...}

POST /query  NOT(city=BJ)            # 删除前 20 行
  -> ids=[3,4,5,6,7,8,9,10,11,13,14,15,17,18,19], count=15

POST /delete {"ids":[0,2,4]}         -> changed=3, aliveCount=17
POST /delete {"where": grade=C}      -> changed=4, aliveCount=13

POST /query NOT(city=BJ)             # 删除后：补集只在存活行内取
  -> ids=[3,5,6,8,9,10,13,14,15,18,19], count=11, aliveCount=13
  # 已删除的行 2(BJ)、4(SH) 都不在结果中：NOT 不会把已删除的非 BJ 行捞回来

POST /query alive                    -> 行 ID 保持原始编号（有“空洞”，不重排）
  -> [1,3,5,6,8,9,10,13,14,15,16,18,19]

POST /delete {"ids":[0,2]} （重复）  -> changed=0   # 幂等

# 重新加载、只删 [0,2,4]、再恢复 id=4（SH 行）后：
POST /query NOT(city=BJ)
  -> ids=[3,4,5,6,7,8,9,10,11,13,14,15,17,18,19], count=15
  # 行 4 恢复后按其原始行号 4 回到补集；存活 18 / 总数 20

POST /load {"csv":"city,grade\n"}    -> {"ok":true,"rowCount":0}
POST /query NOT(eq city=BJ)（空全集）-> {"ids":[],"matchedAlive":0,"aliveCount":0,"totalCount":0}
```

错误码实测：坏 JSON → 400；未知列 → 400；ids 与 where 同时出现 → 400；
未加载即查询 → 409；未知路径 → 404；GET /load → 405。

## 3. 索引空间统计（样例数据，删除 [0,2] 之后）

`GET /stats` 实测摘要（完整字段以接口返回为准）：

| 指标 | 值 |
|---|---|
| rowCount / aliveCount / deletedCount | 20 / 18 / 2 |
| rawDatasetBytes（CSV UTF-8 字节） | 446 |
| dictionaryUtf8Bytes（列名+值名字典） | 220 |
| indexRawBytes（所有值位图 long[] 合计） | 256 |
| indexRleBytes（所有值位图 RLE 合计） | 228 |
| aliveMaskRawBytes / aliveMaskRleBytes | 8 / 5 |
| totalRawBytesInclMasks / totalRleBytesInclMasks | 492 / 458 |
| rleVsRawRatio | 0.8906 |

按列：

| 列 | 基数 | raw | rle | 备注 |
|---|---|---|---|---|
| id | 20（高基数） | 160 | 78 | 每个值 1 行，单点位图 |
| city | 4 | 32 | 30 | 样例中小数据、且城市交错，短游程多 |
| grade | 3 | 24 | 40 | 取值交替，RLE 反而变大（如实统计） |
| active | 2 | 16 | 36 | 近 0101 交替，最坏情形 |
| category | 3 | 24 | 44 | 同上 |

测试中规模更大、分布更典型时的统计（5000 行）：

- 低基数、成块聚集列：raw 2528 B → rle **24 B（0.95%）**；
- 同基数但严格交替列：raw 2528 B → rle 10006 B（395.81%）；
- 高基数唯一值列（5000 张单点位图）：raw 3 160 000 B → rle 29 742 B（0.94%），
  但**绝对体积仍随基数线性膨胀**，这是位图索引的固有代价；
- 0101… 最坏游程列：RLE 为未压缩的 7.82 倍 —— 统计不回避压缩失效场景。

## 4. 测试中发现并修复的真实缺陷（留存）

1. RLE 编码器初版带“整 word 快跳”优化，跨 word 边界时会跳过当前 word 的逐位
   处理，导致单点行（如 len=1023、pos=884）编码后解码丢位。被 RLE 边界测试
   捕获，已重写为“统一 word 快路径 + 混合 word 逐位”的正确实现。
2. 手写 JSON 数字解析用 `isDouble ? Double.valueOf(...) : Long.valueOf(...)`
   三元表达式，两个分支被 Java 统一提升为 double，导致整数 ID 变成 0.0 而无法
   通过 `instanceof Long` 校验。被 HTTP 端到端测试捕获，改为显式 if/else。
3. 读取数字结尾处 `peek()` 在 EOF 抛异常并被 `NumberFormatException` 分支误捕，
   一并改为有界判断。
4. 空参数 `AND` 的单位元应为全集（最初误返回空集），修正为 `Bitmap.full(n)`。
