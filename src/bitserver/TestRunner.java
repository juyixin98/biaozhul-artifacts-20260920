package bitserver;

import com.sun.net.httpserver.HttpServer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;

/**
 * 零依赖自动化测试（纯 main 方法 + 手工断言，失败以非 0 退出码结束）。
 *
 * 覆盖：
 *  1. Bitmap 基础运算；
 *  2. RLE 编解码随机往返（行 ID 不随压缩改变）；
 *  3. CSV 解析；
 *  4. 空全集数据集（0 行）上的 and/or/not/查询/统计；
 *  5. 高基数列（每行一个唯一值）索引与压缩统计；
 *  6. 随机布尔表达式查询 vs 逐行扫描，分四个存活阶段：
 *     未删除 / 按 ID 删除 / 按表达式删除 / 部分恢复；
 *  7. 删除后 NOT 取补语义（NOT 只在存活全集内取补，双重否定）；
 *  8. HTTP 端到端（真实启动 com.sun.net.httpserver.HttpServer，经 HTTP 回环调用）。
 */
public final class TestRunner {

    private static int failures = 0;
    private static int checks = 0;

    public static void main(String[] args) throws Exception {
        testBitmap();
        testRleRoundTrip();
        testRleKnownPatterns();
        testCsv();
        testEmptyUniverse();
        testHighCardinalityAndStats();
        testNotAfterDelete();
        testRowIdStability();
        testRandomExpressionsVsScan();
        testHttpEndToEnd();

        System.out.println();
        System.out.println("================================================");
        if (failures == 0) {
            System.out.println("全部测试通过 ✅  断言数: " + checks);
        } else {
            System.out.println("存在失败 ❌  失败数: " + failures + " / 断言数: " + checks);
            System.exit(1);
        }
    }

    // ---------------------------------------------------------------- bitmap

    private static void testBitmap() {
        section("Bitmap 基础运算");
        Bitmap b = new Bitmap(100);
        b.set(0);
        b.set(63);
        b.set(64);
        b.set(99);
        assertTrue(b.get(0) && b.get(63) && b.get(64) && b.get(99), "置位后可读到 1");
        assertTrue(!b.get(1) && !b.get(65), "未置位为 0");
        eq(b.cardinality(), 4, "cardinality=4");
        eq(b.toArray(), new int[]{0, 63, 64, 99}, "toArray 升序且下标即原始行号");

        // 尾部脏位掩码：长度不是 64 的倍数时 not() 不会造出越界位
        Bitmap b2 = new Bitmap(65);
        b2.set(64);
        Bitmap not2 = b2.not();
        eq(not2.cardinality(), 64, "not(仅第64位) 在 65 长度内为 64");
        assertTrue(!not2.get(64) && not2.get(0), "取补正确");

        Bitmap full = Bitmap.full(130);
        eq(full.cardinality(), 130, "full 位图位数=长度（含非整 word 尾部）");

        Bitmap a = new Bitmap(70);
        a.set(1);
        a.set(69);
        Bitmap c = new Bitmap(70);
        c.set(1);
        c.set(2);
        eq(a.and(c).toArray(), new int[]{1}, "and");
        eq(a.or(c).cardinality(), 3, "or");
        eq(Bitmap.full(70).andNot(a).cardinality(), 68, "andNot（70 位中 a 占 2 位）");

        a.clear(c);
        eq(a.toArray(), new int[]{69}, "clear 后行位消失、长度不变");
        a.merge(c);
        eq(a.cardinality(), 3, "merge 恢复");

        // 空位图
        Bitmap empty = new Bitmap(0);
        eq(empty.cardinality(), 0, "0 长度位图");
        eq(empty.not().length(), 0, "0 长度取补仍为 0 长度");
        eq(Bitmap.full(0).cardinality(), 0, "0 长度 full 为空");
    }

    // ----------------------------------------------------------------- rle

    private static void testRleRoundTrip() {
        section("RLE 随机往返（压缩不改变行 ID）");
        Random rnd = new Random(20260924L);
        int[] lens = {0, 1, 2, 7, 63, 64, 65, 127, 128, 129, 1000, 4097};
        for (int len : lens) {
            for (int trial = 0; trial < 40; trial++) {
                Bitmap bm = new Bitmap(len);
                for (int i = 0; i < len; i++) {
                    if (rnd.nextDouble() < 0.3) {
                        bm.set(i);
                    }
                }
                byte[] coded = RleCodec.encode(bm);
                Bitmap back = RleCodec.decode(coded, len);
                for (int i = 0; i < len; i++) {
                    assertTrue(back.get(i) == bm.get(i),
                            "RLE 往返一致 len=" + len + " rowId=" + i);
                }
                eq(back.cardinality(), bm.cardinality(), "RLE 往返 cardinality 一致");
                eq(back.toArray(), bm.toArray(), "RLE 往返行 ID 数组完全一致");
            }
        }
    }

    private static void testRleKnownPatterns() {
        section("RLE 边界模式");
        int[] patterns = {
                0, 1, 2, 3, 62, 63, 64, 65, 66, 127, 128, 129, 255, 256, 1023
        };
        for (int len : patterns) {
            for (int p = 0; p < len; p++) {
                checkSingleRle(len, p, 1);
                checkSingleRle(len, p, 0);
            }
            // 交替 0101...
            Bitmap alt = new Bitmap(len);
            for (int i = 0; i < len; i += 2) alt.set(i);
            Bitmap back = RleCodec.decode(RleCodec.encode(alt), len);
            eq(back.toArray(), alt.toArray(), "交替模式往返 len=" + len);
            // 全 0 / 全 1
            Bitmap all0 = new Bitmap(len);
            Bitmap all1 = Bitmap.full(len);
            eq(RleCodec.decode(RleCodec.encode(all0), len).cardinality(), 0, "全 0 往返");
            eq(RleCodec.decode(RleCodec.encode(all1), len).cardinality(), len, "全 1 往返");
            // 全 0 的编码应很短（firstBit + 一个 varint）
            int codedLen = RleCodec.encode(all0).length;
            assertTrue(codedLen <= 3, "全 0 位图编码极短（实际 " + codedLen + " 字节, len=" + len + "）");
        }
    }

    private static void checkSingleRle(int len, int pos, int first) {
        Bitmap bm = new Bitmap(len);
        if (first == 1) {
            bm.set(pos);
        } else {
            for (int i = 0; i < len; i++) {
                if (i != pos) bm.set(i);
            }
        }
        Bitmap back = RleCodec.decode(RleCodec.encode(bm), len);
        eq(back.toArray(), bm.toArray(), "单位/单洞往返 len=" + len + " pos=" + pos);
    }

    // ----------------------------------------------------------------- csv

    private static void testCsv() {
        section("CSV 解析");
        Csv.Table t = Csv.parse("city,grade\nBJ,A\nSH,B\n");
        eq(t.headers, List.of("city", "grade"), "表头");
        eq(t.rows.size(), 2, "两行数据");
        eq(t.rows.get(0), List.of("BJ", "A"), "首行内容");

        Csv.Table quoted = Csv.parse("a,b\n\"x,y\", \"line\n2\"\n");
        eq(quoted.rows.get(0).get(0), "x,y", "引号内逗号");
        eq(quoted.rows.get(0).get(1), " line\n2", "引号内换行（前置空格保留）");

        Csv.Table crlf = Csv.parse("a\r\nv\r\n");
        eq(crlf.rows.get(0), List.of("v"), "CRLF 行尾");

        expectError(() -> Csv.parse(""), "空内容报错");
        expectError(() -> Csv.parse("a,a\n1,2\n"), "重复列名报错");
        expectError(() -> Csv.parse("a,b\n1\n"), "列数不一致报错");
        expectError(() -> Csv.parse("a\n\"unclosed"), "未闭合引号报错");
    }

    // ------------------------------------------------------- 空全集数据集

    private static void testEmptyUniverse() {
        section("空全集（0 行数据集）");
        BitmapIndex idx = BitmapIndex.build(List.of("city", "grade"), List.of());
        eq(idx.rowCount(), 0, "0 行索引");
        Bitmap alive = Bitmap.full(0);
        QueryEngine engine = new QueryEngine(idx);

        Map<String, Object> eqCity = expr("eq", "col", "city", "value", "BJ");
        eq(engine.query(eqCity, alive).toArray(), new int[0], "空全集 eq 命中空");
        eq(engine.query(expr("or"), alive).cardinality(), 0, "空全集 or 为空");
        eq(engine.query(expr("and"), alive).cardinality(), 0, "空全集 and(全集) 与存活求交为空");
        eq(engine.query(expr("not", "arg", eqCity), alive).cardinality(), 0,
                "空全集 NOT 也是空（不会凭空造出行）");
        eq(engine.query(expr("alive"), alive).cardinality(), 0, "空全集存活数为 0");
        eq(alive.cardinality(), 0, "空全集存活掩码基数 0");
        eq(alive.length(), 0, "空全集掩码长度 0");

        // 通过 JSON CSV 构建：只有表头
        IndexService svc = new IndexService();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("csv", "city,grade\n");
        eq(svc.load(body), 0, "表头 CSV 加载 0 行");
        Map<String, Object> stats = svc.stats();
        eq(((Number) stats.get("rowCount")).intValue(), 0, "统计 rowCount=0");
        eq(((Number) stats.get("aliveCount")).intValue(), 0, "统计 aliveCount=0");
        eq(((Number) stats.get("deletedCount")).intValue(), 0, "统计 deletedCount=0");
    }

    // ------------------------------------------------- 高基数列 + 空间统计

    private static void testHighCardinalityAndStats() {
        section("高基数列与索引空间统计");
        int n = 5000;
        List<String> cols = List.of("city", "cityCyclic", "sku");
        String[] cities = {"BJ", "SH", "GZ", "SZ"};
        List<List<String>> rows = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            // city: 成块聚集（低基数列的典型分布，长游程 -> RLE 友好）
            String clusteredCity = cities[(i / 1250) % cities.length];
            // cityCyclic: 同是 4 个值但严格交替（短游程 -> RLE 不友好，如实统计）
            String cyclicCity = cities[i % cities.length];
            rows.add(List.of(clusteredCity, cyclicCity, String.format("SKU%05d", i)));
        }
        BitmapIndex idx = BitmapIndex.build(cols, rows);
        eq(idx.column("city").values.size(), 4, "city 4 个不同值");
        eq(idx.column("sku").values.size(), n, "sku 高基数：每行一个不同值");

        int cityRaw = idx.column("city").rawBytes;
        int cityRle = idx.column("city").compressedBytes;
        int cyclicRaw = idx.column("cityCyclic").rawBytes;
        int cyclicRle = idx.column("cityCyclic").compressedBytes;
        int skuRaw = idx.column("sku").rawBytes;
        int skuRle = idx.column("sku").compressedBytes;
        System.out.printf("    city 低基数(成块聚集): raw=%d B, rle=%d B (%.2f%%)%n",
                cityRaw, cityRle, 100.0 * cityRle / cityRaw);
        System.out.printf("    cityCyclic 低基数(严格交替): raw=%d B, rle=%d B (%.2f%%)%n",
                cyclicRaw, cyclicRle, 100.0 * cyclicRle / cyclicRaw);
        System.out.printf("    sku  高基数(5000 个单点位图): raw=%d B, rle=%d B (%.2f%%)%n",
                skuRaw, skuRle, 100.0 * skuRle / skuRaw);
        assertTrue(cityRle < cityRaw, "成块分布的低基数列 RLE 明显小于未压缩");
        assertTrue(skuRle < skuRaw, "单点位图 RLE 仍小于整 word 位图");
        // 同为 4 个不同值，分布决定压缩率：成块 vs 严格交替
        assertTrue(cityRle < cyclicRle, "分布影响 RLE：成块 << 交替（如实统计，不粉饰）");

        // 高基数列真正的空间代价：相比低基数列，每多一个值就要多存一张位图，
        // 总索引体积随基数线性膨胀（未压缩口径尤其明显）。
        assertTrue(skuRaw > cityRaw * 100,
                "高基数列索引体积远大于低基数列: skuRaw=" + skuRaw + " cityRaw=" + cityRaw);

        // 值只有两个但位型为 0101...（最坏游程）
        List<List<String>> altRows = new ArrayList<>();
        for (int i = 0; i < 1000; i++) {
            altRows.add(List.of(i % 2 == 0 ? "X" : "Y"));
        }
        BitmapIndex altIdx = BitmapIndex.build(List.of("flag"), altRows);
        int altRaw = altIdx.column("flag").rawBytes;
        int altRle = altIdx.column("flag").compressedBytes;
        System.out.printf("    flag 交替最坏游程: raw=%d B, rle=%d B (%.1f%% —— 压缩率差是真实统计)%n",
                altRaw, altRle, 100.0 * altRle / altRaw);
        assertTrue(altRle >= altRaw, "最坏游程下 RLE 不占优，统计如实反映");
    }

    // ---------------------------------------------- 删除后 NOT 取补语义

    private static void testNotAfterDelete() {
        section("删除后的 NOT 取补语义");
        List<String> cols = List.of("city", "grade");
        List<List<String>> rows = new ArrayList<>();
        rows.add(List.of("BJ", "A")); // 0
        rows.add(List.of("BJ", "B")); // 1
        rows.add(List.of("SH", "A")); // 2
        rows.add(List.of("SH", "B")); // 3
        rows.add(List.of("GZ", "A")); // 4
        IndexService svc = serviceWith(cols, rows);

        // 删除行 0、2、4（偶数行）
        svc.delete(List.of(0L, 2L, 4L), null);
        Map<String, Object> stats = svc.stats();
        eq(stats.get("aliveCount"), 2, "存活 2 行");
        eq(stats.get("deletedCount"), 3, "删除 3 行");

        QueryEngine engine = new QueryEngine(BitmapIndex.build(cols, rows));
        // 直接用服务内部引擎语义验证：通过 query 接口间接测
        Map<String, Object> notCityBJ = expr("not", "arg", expr("eq", "col", "city", "value", "BJ"));
        IndexService.QueryResult r = svc.query(Map.of("where", notCityBJ));
        // 存活行只有 1(BJ/B) 与 3(SH/B)；NOT(city=BJ) 必须 = 存活中的非 BJ = {3}
        eq(r.ids, new int[]{3}, "NOT(city=BJ) 只在存活全集内取补 -> {3}，已删除行 2/4 不会回来");

        // NOT(NOT(city=BJ)) = 存活 AND city=BJ = {1}
        Map<String, Object> notNot = expr("not", "arg", notCityBJ);
        eq(svc.query(Map.of("where", notNot)).ids, new int[]{1}, "双重 NOT 等价存活内原谓词");

        // alive 全集显式查询
        eq(svc.query(Map.of("where", expr("alive"))).ids, new int[]{1, 3}, "alive 全集为 {1,3}");

        // OR(NOT ..., city=BJ) 同样不允许漏出删除行
        Map<String, Object> weird = expr("or", "args",
                List.of(notCityBJ, expr("eq", "col", "city", "value", "GZ")));
        int[] got = svc.query(Map.of("where", weird)).ids;
        for (int id : got) {
            assertTrue(id == 1 || id == 3, "复杂表达式绝不返回已删除行，实际出现 id=" + id);
        }
        eq(got, new int[]{3}, "OR 结果仍只有存活行 {3}（GZ 行 4 已删）");

        // 恢复行 2 后，NOT(city=BJ) 变成 {2,3}
        svc.restore(List.of(2L), null);
        eq(svc.query(Map.of("where", notCityBJ)).ids, new int[]{2, 3}, "恢复后取补随存活全集变化");
    }

    // ------------------------------------------------- 行 ID 稳定性

    private static void testRowIdStability() {
        section("删除/压缩后行 ID 不变化");
        List<String> cols = List.of("city");
        List<List<String>> rows = new ArrayList<>();
        for (int i = 0; i < 200; i++) {
            rows.add(List.of(i % 3 == 0 ? "BJ" : (i % 3 == 1 ? "SH" : "GZ")));
        }
        IndexService svc = serviceWith(cols, rows);
        List<Object> deleteIds = new ArrayList<>();
        for (int i = 0; i < 200; i += 2) {
            deleteIds.add((long) i);
        }
        svc.delete(deleteIds, null);
        // city=BJ 且存活的行：i%3==0 且 i 为奇数 -> i = 3,9,15,...,195
        List<Integer> expected = new ArrayList<>();
        for (int i = 0; i < 200; i++) {
            if (i % 3 == 0 && i % 2 == 1) expected.add(i);
        }
        int[] exp = expected.stream().mapToInt(Integer::intValue).toArray();
        int[] got = svc.query(Map.of("where", expr("eq", "col", "city", "value", "BJ"))).ids;
        eq(got, exp, "删除一半行后，剩余行仍保留原始行 ID（无压缩式重编号）");
        assertTrue(got[0] == 3, "首个命中是原始行号 3 而不是被重编号后的 0");
    }

    // ------------------------------- 随机布尔表达式 vs 行扫描（核心验收）

    private static void testRandomExpressionsVsScan() {
        section("随机布尔表达式 vs 逐行扫描（4 个存活阶段 × 600 表达式）");
        Random rnd = new Random(424242L);
        int n = 400;
        String[] cities = {"BJ", "SH", "GZ", "SZ"};
        String[] grades = {"A", "B", "C"};
        String[] flags = {"true", "false"};
        String[] cats = {"x", "y", "z", "w", "q", "t"};
        List<String> columns = List.of("city", "grade", "active", "category", "sku");

        List<List<String>> data = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            data.add(List.of(
                    cities[rnd.nextInt(cities.length)],
                    grades[rnd.nextInt(grades.length)],
                    flags[rnd.nextInt(flags.length)],
                    cats[rnd.nextInt(cats.length)],
                    String.format("SKU%04d", i))); // 高基数列
        }
        BitmapIndex idx = BitmapIndex.build(columns, data);
        QueryEngine engine = new QueryEngine(idx);
        Bitmap alive = Bitmap.full(n);

        // 阶段 1：无删除
        runRandomPhase(engine, alive, data, columns, rnd, 600, "阶段1 无删除");

        // 阶段 2：按 ID 固定删除约 1/4
        List<Object> idList = new ArrayList<>();
        for (int i = 0; i < n; i += 4) {
            idList.add((long) i);
            alive.clear(singleBitmap(n, i));
        }
        runRandomPhase(engine, alive, data, columns, rnd, 600, "阶段2 按 ID 删除 1/4");

        // 阶段 3：再按表达式删除 city=BJ AND active=true（行扫描口径）
        for (int i = 0; i < n; i++) {
            if (alive.get(i) && data.get(i).get(0).equals("BJ") && data.get(i).get(2).equals("true")) {
                alive.clear(singleBitmap(n, i));
            }
        }
        runRandomPhase(engine, alive, data, columns, rnd, 600, "阶段3 再按表达式删除");

        // 阶段 4：恢复阶段 2 删除的前 10 个 id
        int restored = 0;
        for (int i = 0; i < n && restored < 10; i += 4) {
            alive.set(i);
            restored++;
        }
        runRandomPhase(engine, alive, data, columns, rnd, 600, "阶段4 部分恢复");
    }

    private static void runRandomPhase(QueryEngine engine, Bitmap alive,
                                       List<List<String>> data, List<String> columns,
                                       Random rnd, int count, String phase) {
        int mismatches = 0;
        for (int t = 0; t < count; t++) {
            Map<String, Object> expr = randomExpr(rnd, columns, data, 3);
            Bitmap bm = engine.query(expr, alive);
            boolean[] scan = scanEval(expr, data, alive, columns);
            int[] bmIds = bm.toArray();
            List<Integer> scanIds = new ArrayList<>();
            for (int i = 0; i < scan.length; i++) {
                if (scan[i]) scanIds.add(i);
            }
            if (bmIds.length != scanIds.size()) {
                mismatches++;
                if (mismatches <= 3) {
                    System.out.println("    基数不一致 [" + phase + "] expr=" + Json.write(expr)
                            + " bitmap=" + bmIds.length + " scan=" + scanIds.size());
                }
                continue;
            }
            for (int i = 0; i < bmIds.length; i++) {
                if (bmIds[i] != scanIds.get(i)) {
                    mismatches++;
                    if (mismatches <= 3) {
                        System.out.println("    行 ID 不一致 [" + phase + "] expr=" + Json.write(expr));
                    }
                    break;
                }
            }
        }
        eq(mismatches, 0, phase + "：" + count + " 条随机表达式与行扫描完全一致");
        System.out.printf("    %s: alive=%d/%d, %d 条随机表达式全部匹配%n",
                phase, alive.cardinality(), data.size(), count);
    }

    /** 递归生成随机表达式（叶子只使用真实存在的列/值，深度受限）。 */
    private static Map<String, Object> randomExpr(Random rnd, List<String> columns,
                                                  List<List<String>> data, int depth) {
        if (depth == 0 || rnd.nextInt(3) == 0) {
            String col = columns.get(rnd.nextInt(columns.size()));
            int ci = columns.indexOf(col);
            if (rnd.nextInt(5) == 0) {
                // in：1~3 个真实值
                List<Object> vals = new ArrayList<>();
                int k = 1 + rnd.nextInt(3);
                for (int i = 0; i < k; i++) {
                    vals.add(data.get(rnd.nextInt(data.size())).get(ci));
                }
                return expr("in", "col", col, "values", vals);
            }
            Object val = data.get(rnd.nextInt(data.size())).get(ci);
            return expr("eq", "col", col, "value", val);
        }
        int kind = rnd.nextInt(4);
        switch (kind) {
            case 0: {
                int k = 2 + rnd.nextInt(3);
                List<Object> args = new ArrayList<>();
                for (int i = 0; i < k; i++) args.add(randomExpr(rnd, columns, data, depth - 1));
                return expr("and", "args", args);
            }
            case 1: {
                int k = 2 + rnd.nextInt(3);
                List<Object> args = new ArrayList<>();
                for (int i = 0; i < k; i++) args.add(randomExpr(rnd, columns, data, depth - 1));
                return expr("or", "args", args);
            }
            case 2:
                return expr("not", "arg", randomExpr(rnd, columns, data, depth - 1));
            default:
                return expr("alive");
        }
    }

    /** 行扫描口径的参考实现（NOT 只在存活行内取补）。 */
    private static boolean[] scanEval(Map<String, Object> expr, List<List<String>> data,
                                      Bitmap alive, List<String> columns) {
        int n = data.size();
        boolean[] r = scanNode(expr, data, alive, columns, n);
        for (int i = 0; i < n; i++) {
            r[i] = r[i] && alive.get(i);
        }
        return r;
    }

    private static boolean[] scanNode(Map<String, Object> e, List<List<String>> data,
                                      Bitmap alive, List<String> columns, int n) {
        String op = (String) e.get("op");
        boolean[] r = new boolean[n];
        switch (op) {
            case "eq": {
                int ci = columns.indexOf(e.get("col"));
                String v = (String) e.get("value");
                for (int i = 0; i < n; i++) r[i] = data.get(i).get(ci).equals(v);
                return r;
            }
            case "in": {
                int ci = columns.indexOf(e.get("col"));
                @SuppressWarnings("unchecked")
                List<Object> vals = (List<Object>) e.get("values");
                for (int i = 0; i < n; i++) {
                    for (Object v : vals) {
                        if (data.get(i).get(ci).equals(v)) {
                            r[i] = true;
                            break;
                        }
                    }
                }
                return r;
            }
            case "and": {
                @SuppressWarnings("unchecked")
                List<Object> args = (List<Object>) e.get("args");
                Arrays.fill(r, true);
                for (Object a : args) {
                    boolean[] b = scanNode((Map<String, Object>) a, data, alive, columns, n);
                    for (int i = 0; i < n; i++) r[i] = r[i] && b[i];
                }
                return r;
            }
            case "or": {
                @SuppressWarnings("unchecked")
                List<Object> args = (List<Object>) e.get("args");
                for (Object a : args) {
                    boolean[] b = scanNode((Map<String, Object>) a, data, alive, columns, n);
                    for (int i = 0; i < n; i++) r[i] = r[i] || b[i];
                }
                return r;
            }
            case "not": {
                boolean[] inner = scanNode((Map<String, Object>) e.get("arg"), data, alive, columns, n);
                for (int i = 0; i < n; i++) r[i] = alive.get(i) && !inner[i]; // 与引擎同语义
                return r;
            }
            case "alive": {
                for (int i = 0; i < n; i++) r[i] = alive.get(i);
                return r;
            }
            default:
                throw new IllegalArgumentException("扫描器不支持 op: " + op);
        }
    }

    private static Bitmap singleBitmap(int n, int i) {
        Bitmap b = new Bitmap(n);
        b.set(i);
        return b;
    }

    // ------------------------------------------------------------ HTTP 端到端

    private static void testHttpEndToEnd() throws Exception {
        section("HTTP 端到端（真实 HttpServer + HTTP 回环）");
        IndexService svc = new IndexService();
        HttpServer server = Main.startServer(svc, 0);
        int port = server.getAddress().getPort();
        String base = "http://127.0.0.1:" + port;
        try {
            HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();

            // 未加载时 /query -> 409
            HttpResponse<String> pre = post(client, base + "/query", "{\"where\":{\"op\":\"alive\"}}");
            eq(pre.statusCode(), 409, "未加载查询返回 409");

            HttpResponse<String> health = get(client, base + "/health");
            eq(health.statusCode(), 200, "health 200");
            assertTrue(health.body().contains("\"no-data\""), "健康状态 no-data");

            // 加载 CSV
            String loadBody = "{\"csv\":\"city,grade\\nBJ,A\\nBJ,B\\nSH,A\\nSH,B\\nGZ,A\\n\"}";
            HttpResponse<String> loaded = post(client, base + "/load", loadBody);
            eq(loaded.statusCode(), 200, "load 200");
            assertTrue(loaded.body().contains("\"rowCount\":5"), "加载 5 行: " + loaded.body());

            // 查询 city=BJ -> [0,1]
            HttpResponse<String> q1 = post(client, base + "/query",
                    "{\"where\":{\"op\":\"eq\",\"col\":\"city\",\"value\":\"BJ\"}}");
            eq(q1.statusCode(), 200, "query 200");
            assertTrue(q1.body().contains("\"ids\":[0,1]"), "city=BJ -> [0,1]: " + q1.body());
            assertTrue(q1.body().contains("\"matchedAlive\":2"), "matchedAlive=2");

            // 删除 id=0,2
            HttpResponse<String> del = post(client, base + "/delete", "{\"ids\":[0,2]}");
            eq(del.statusCode(), 200, "delete 200");
            assertTrue(del.body().contains("\"changed\":2"), "删除 2 行: " + del.body());

            // NOT(city=BJ) 存活行：存活为 1,3,4；非 BJ = {3,4}
            HttpResponse<String> q2 = post(client, base + "/query",
                    "{\"where\":{\"op\":\"not\",\"arg\":{\"op\":\"eq\",\"col\":\"city\",\"value\":\"BJ\"}}}");
            assertTrue(q2.body().contains("\"ids\":[3,4]"), "删除后 NOT 取补 -> [3,4]: " + q2.body());

            // where 删除：删除 grade=A 的存活行（行 4；行 0、2 已删）
            HttpResponse<String> del2 = post(client, base + "/delete",
                    "{\"where\":{\"op\":\"eq\",\"col\":\"grade\",\"value\":\"A\"}}");
            assertTrue(del2.body().contains("\"changed\":1"), "按表达式仅新删除 1 行（幂等）: " + del2.body());

            // 恢复 id=0
            HttpResponse<String> res = post(client, base + "/restore", "{\"ids\":[0]}");
            assertTrue(res.body().contains("\"changed\":1"), "恢复 1 行: " + res.body());
            HttpResponse<String> resDup = post(client, base + "/restore", "{\"ids\":[0]}");
            assertTrue(resDup.body().contains("\"changed\":0"), "重复恢复幂等 changed=0");

            // limit
            HttpResponse<String> q3 = post(client, base + "/query",
                    "{\"where\":{\"op\":\"alive\"},\"limit\":2}");
            assertTrue(q3.body().contains("\"count\":2") && q3.body().contains("\"truncated\":true"),
                    "limit 截断: " + q3.body());

            // stats
            HttpResponse<String> st = get(client, base + "/stats");
            eq(st.statusCode(), 200, "stats 200");
            @SuppressWarnings("unchecked")
            Map<String, Object> stats = (Map<String, Object>) Json.parse(st.body());
            eq(((Number) stats.get("rowCount")).intValue(), 5, "stats rowCount=5");
            assertTrue(stats.containsKey("indexRawBytes") && stats.containsKey("indexRleBytes"),
                    "统计含未压缩/压缩字节");
            assertTrue(((Number) stats.get("aliveMaskRawBytes")).intValue() > 0, "存活掩码原始字节>0");
            System.out.println("    stats 摘要: rowCount=" + stats.get("rowCount")
                    + " indexRawBytes=" + stats.get("indexRawBytes")
                    + " indexRleBytes=" + stats.get("indexRleBytes")
                    + " ratio=" + stats.get("rleVsRawRatio"));

            // 错误请求：坏 JSON / 未知列 / 同时给 ids 和 where
            eq(post(client, base + "/query", "{not json").statusCode(), 400, "坏 JSON -> 400");
            eq(post(client, base + "/query",
                    "{\"where\":{\"op\":\"eq\",\"col\":\"nope\",\"value\":\"x\"}}").statusCode(), 400,
                    "未知列 -> 400");
            eq(post(client, base + "/delete", "{\"ids\":[0],\"where\":{\"op\":\"alive\"}}").statusCode(),
                    400, "同时给 ids+where -> 400");
            eq(post(client, base + "/delete", "{}").statusCode(), 400, "都不给 -> 400");
            eq(get(client, base + "/nope").statusCode(), 404, "未知路径 -> 404");
            eq(client.send(HttpRequest.newBuilder(URI.create(base + "/load")).GET().build(),
                    HttpResponse.BodyHandlers.ofString()).statusCode(), 405, "GET /load -> 405");

            // columns+rows JSON 加载（整体替换，新数据集从行 0 重新编号）
            HttpResponse<String> reload = post(client, base + "/load",
                    "{\"columns\":[\"k\"],\"rows\":[[\"a\"],[\"b\"]]}");
            assertTrue(reload.body().contains("\"rowCount\":2"), "JSON 重载 2 行");
            HttpResponse<String> q4 = post(client, base + "/query",
                    "{\"where\":{\"op\":\"eq\",\"col\":\"k\",\"value\":\"a\"}}");
            assertTrue(q4.body().contains("\"ids\":[0]"), "重载后行 ID 从 0 重新按新数据编号");
        } finally {
            server.stop(0);
        }
    }

    // --------------------------------------------------------------- 工具方法

    private static IndexService serviceWith(List<String> cols, List<List<String>> rows) {
        IndexService svc = new IndexService();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("columns", cols);
        body.put("rows", rows);
        svc.load(body);
        return svc;
    }

    /** 构造表达式对象：expr("eq","col","city","value","BJ")（首参数自动作为 op）。 */
    private static Map<String, Object> expr(Object... kv) {
        if (kv.length % 2 != 1) {
            throw new IllegalArgumentException("expr 需要 op + 偶数个键值对");
        }
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("op", kv[0]);
        for (int i = 1; i < kv.length; i += 2) {
            m.put((String) kv[i], kv[i + 1]);
        }
        return m;
    }

    private static HttpResponse<String> post(HttpClient client, String url, String body) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url))
                .header("Content-Type", "application/json; charset=utf-8")
                .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private static HttpResponse<String> get(HttpClient client, String url) throws Exception {
        return client.send(HttpRequest.newBuilder(URI.create(url)).GET().build(),
                HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private static void section(String name) {
        System.out.println("── " + name);
    }

    private static void eq(Object actual, Object expected, String msg) {
        checks++;
        boolean ok;
        String actualShow;
        String expectedShow;
        if (actual instanceof int[] a && expected instanceof int[] e) {
            ok = Arrays.equals(a, e);
            actualShow = Arrays.toString(a);
            expectedShow = Arrays.toString(e);
        } else if (actual instanceof List<?> a && expected instanceof List<?> e) {
            ok = a.equals(e);
            actualShow = a.toString();
            expectedShow = e.toString();
        } else {
            ok = java.util.Objects.equals(actual, expected);
            actualShow = String.valueOf(actual);
            expectedShow = String.valueOf(expected);
        }
        if (!ok) {
            failures++;
            System.out.println("  ❌ " + msg + "\n     期望: " + expectedShow + "\n     实际: " + actualShow);
        }
    }

    private static void assertTrue(boolean cond, String msg) {
        checks++;
        if (!cond) {
            failures++;
            System.out.println("  ❌ " + msg);
        }
    }

    private static void expectError(Runnable r, String msg) {
        checks++;
        try {
            r.run();
            failures++;
            System.out.println("  ❌ " + msg + "（未抛出异常）");
        } catch (RuntimeException ignored) {
            // 预期路径
        }
    }
}
