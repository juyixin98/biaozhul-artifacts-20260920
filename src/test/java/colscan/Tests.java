package colscan;

import colscan.json.Json;
import colscan.query.AggSpec;
import colscan.query.Filter;
import colscan.query.Pruner;
import colscan.query.QueryEngine;
import colscan.query.QueryRequest;
import colscan.store.Catalog;
import colscan.store.ColumnFile;
import colscan.store.ColumnStats;
import colscan.store.ShardStats;
import colscan.store.Types;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 零依赖自动化测试（纯 main + 断言）。
 * 覆盖：列文件 NULL 往返、统计计算、裁剪判定（全 NULL / 边界相等 / 统计缺失）、
 * 端到端裁剪与全扫描一致性 + 读取字节对照、NULL 不等于 0、HTTP 接口。
 */
public final class Tests {

    private static int passed;
    private static int failed;
    private static final List<String> failures = new ArrayList<>();

    public static void main(String[] args) throws Exception {
        testColumnFileNullRoundTrip();
        testColumnFileMixedLong();
        testStatsCompute();
        testPrunerDecisions();
        testEndToEndAcceptance();
        testNullNeverZeroAggregates();
        testDoubleType();
        testStatsFilePhysicallyMissing();
        testHttpEndToEnd();

        System.out.println();
        System.out.println("========================================");
        System.out.println("通过: " + passed + "，失败: " + failed);
        if (failed > 0) {
            for (String f : failures) System.out.println("  [FAIL] " + f);
            System.exit(1);
        }
        System.out.println("全部测试通过");
    }

    private static void check(String name, boolean cond, String detail) {
        if (cond) {
            passed++;
            System.out.println("  [PASS] " + name);
        } else {
            failed++;
            failures.add(name + " — " + detail);
            System.out.println("  [FAIL] " + name + " — " + detail);
        }
    }

    private static void check(String name, boolean cond) {
        check(name, cond, "断言失败");
    }

    // ---------------- 列文件 ----------------

    private static void testColumnFileNullRoundTrip() throws IOException {
        Path tmp = Files.createTempDirectory("colscan-test-");
        Path f = tmp.resolve("allnull.col");
        Object[] in = {null, null, null, null};
        ColumnFile.write(f, Types.LONG, in);
        ColumnFile.ReadResult r = ColumnFile.read(f);
        boolean allNull = r.values.length == 4;
        for (Object v : r.values) allNull &= (v == null);
        check("全NULL列往返：4 个值全部为 null（绝不出现 0）", allNull);
        // 文件大小核对：magic7+type1+rows4+nulls4+bitmap1+values0 = 17
        check("全NULL列文件不含任何 value 字节（17 字节）",
                Files.size(f) == 17, "实际=" + Files.size(f));
    }

    private static void testColumnFileMixedLong() throws IOException {
        Path tmp = Files.createTempDirectory("colscan-test-");
        Path f = tmp.resolve("mixed.col");
        Object[] in = {null, 5L, null, -7L, 0L};
        ColumnFile.write(f, Types.LONG, in);
        ColumnFile.ReadResult r = ColumnFile.read(f);
        boolean ok = r.values[0] == null && r.values[1].equals(5L)
                && r.values[2] == null && r.values[3].equals(-7L)
                && r.values[4].equals(0L);
        check("含 NULL/0/负数的列往返，NULL 位置与值均正确", ok,
                listOf(r.values).toString());
        check("列读取字节数等于文件实际大小",
                r.bytesRead == Files.size(f), "read=" + r.bytesRead
                        + " size=" + Files.size(f));
    }

    private static List<Object> listOf(Object[] a) {
        List<Object> l = new ArrayList<>();
        for (Object o : a) l.add(o);
        return l;
    }

    // ---------------- 统计 ----------------

    private static void testStatsCompute() {
        Map<String, String> types = Map.of("v", Types.LONG);
        Map<String, Object[]> data = Map.of("v",
                new Object[]{null, 3L, null, 9L, 3L});
        ShardStats s = ShardStats.compute(5, types, data);
        check("统计 NULL 计数=2", s.columns.get("v").nullCount == 2);
        check("统计 min=3", s.columns.get("v").min.equals(3L));
        check("统计 max=9", s.columns.get("v").max.equals(9L));

        ShardStats allNull = ShardStats.compute(3, types,
                Map.of("v", new Object[]{null, null, null}));
        ColumnStats cs = allNull.columns.get("v");
        check("全NULL分片 min/max 为 null（不是 0）",
                cs.nullCount == 3 && cs.min == null && cs.max == null
                        && cs.allNull(),
                "min=" + cs.min + " max=" + cs.max);
    }

    // ---------------- 裁剪判定矩阵（含边界相等） ----------------

    private static void testPrunerDecisions() {
        ColumnStats range = new ColumnStats(0, 10L, 20L);
        ColumnStats withNulls = new ColumnStats(2, 10L, 20L);
        ColumnStats tenOnly = new ColumnStats(2, 10L, 10L);
        ColumnStats allNull = new ColumnStats(4, null, null);

        check("EQ 值==min（边界相等）必须扫描",
                !Pruner.decide(range, new Filter("v", Filter.EQ, 10L)).prune);
        check("EQ 值==max（边界相等）必须扫描",
                !Pruner.decide(range, new Filter("v", Filter.EQ, 20L)).prune);
        check("EQ 值严格小于 min 可裁剪",
                Pruner.decide(range, new Filter("v", Filter.EQ, 9L)).prune);
        check("EQ 值严格大于 max 可裁剪",
                Pruner.decide(range, new Filter("v", Filter.EQ, 21L)).prune);
        check("EQ 值在范围内扫描",
                !Pruner.decide(range, new Filter("v", Filter.EQ, 15L)).prune);

        check("LT min==值（边界相等，x<10 最小值行也不命中且无更小值）可裁剪",
                Pruner.decide(range, new Filter("v", Filter.LT, 10L)).prune);
        check("LT min>值 可裁剪",
                Pruner.decide(range, new Filter("v", Filter.LT, 9L)).prune);
        check("LT max==值（边界相等，可能存在更小值）必须扫描",
                !Pruner.decide(range, new Filter("v", Filter.LT, 20L)).prune);
        check("LT 值严格大于 max 时所有值都更小，全部命中，扫描",
                !Pruner.decide(range, new Filter("v", Filter.LT, 21L)).prune);

        check("LE min==值（边界相等，x<=10 命中 min 行）必须扫描",
                !Pruner.decide(range, new Filter("v", Filter.LE, 10L)).prune);
        check("LE min>值 可裁剪",
                Pruner.decide(range, new Filter("v", Filter.LE, 9L)).prune);

        check("GT min==值（边界相等，x>10 可能命中更大值）必须扫描",
                !Pruner.decide(range, new Filter("v", Filter.GT, 10L)).prune);
        check("GT max==值 时所有值都不大于它，可裁剪",
                Pruner.decide(range, new Filter("v", Filter.GT, 20L)).prune);
        check("GT max>值 扫描",
                !Pruner.decide(range, new Filter("v", Filter.GT, 15L)).prune);

        check("GE max==值（边界相等，x>=20 命中 max 行）必须扫描",
                !Pruner.decide(range, new Filter("v", Filter.GE, 20L)).prune);
        check("GE max<值 可裁剪",
                Pruner.decide(range, new Filter("v", Filter.GE, 21L)).prune);

        check("NE 常量分片且常量==值（所有非NULL行都不命中）可裁剪",
                Pruner.decide(tenOnly, new Filter("v", Filter.NE, 10L)).prune);
        check("NE 常量分片但常量!=值（所有非NULL行都命中）必须扫描",
                !Pruner.decide(tenOnly, new Filter("v", Filter.NE, 9L)).prune);
        check("NE 范围分片 -> 扫描（范围内可能存在 != 值的其他值）",
                !Pruner.decide(range, new Filter("v", Filter.NE, 15L)).prune);

        check("含 NULL 不改变范围判定：EQ 10 边界相等仍扫描",
                !Pruner.decide(withNulls, new Filter("v", Filter.EQ, 10L)).prune);
        check("全 NULL 分片对任何比较谓词都可裁剪（NULL 不参与比较）",
                Pruner.decide(allNull, new Filter("v", Filter.EQ, 0L)).prune
                        && Pruner.decide(allNull, new Filter("v", Filter.GT, -100L)).prune
                        && Pruner.decide(allNull, new Filter("v", Filter.NE, 0L)).prune);

        check("统计缺失（null）必须扫描",
                !Pruner.decide(null, new Filter("v", Filter.EQ, 10L)).prune
                        && Pruner.decide(null, new Filter("v", Filter.EQ, 10L))
                                .reason.equals(Pruner.SCAN_STATS_MISSING));
    }

    // ---------------- 端到端验收场景 ----------------

    private static Catalog buildAcceptanceTable(Path dir, String table) throws IOException {
        Catalog cat = new Catalog(dir);
        Map<String, String> schema = new LinkedHashMap<>();
        schema.put("id", Types.LONG);
        schema.put("amount", Types.LONG);

        int n = 64; // 每片 64 行，让数据节省大于统计读取成本
        // shard0：低值区间 [1,4]，循环填充
        cat.appendShard(table, schema, Map.of(
                "id", seq(0, n, k -> (long) k + 1),
                "amount", seq(0, n, k -> (long) (k % 4 + 1))), true);
        // shard1：边界相等 + NULL（amount 非空值全是 10，另有 2 个 NULL）
        cat.appendShard(table, schema, Map.of(
                "id", seq(0, n, k -> (long) n + k + 1),
                "amount", seq(0, n, k -> (k < n - 2) ? 10L : null)), true);
        // shard2：高值区间 [100,400]
        cat.appendShard(table, schema, Map.of(
                "id", seq(0, n, k -> (long) (2 * n) + k + 1),
                "amount", seq(0, n, k -> (long) (100 * (k % 4 + 1)))), true);
        // shard3：全 NULL
        cat.appendShard(table, schema, Map.of(
                "id", seq(0, n, k -> (long) (3 * n) + k + 1),
                "amount", seq(0, n, k -> null)), true);
        // shard4：统计缺失（computeStats=false），物理值 [50,80]
        cat.appendShard(table, schema, Map.of(
                "id", seq(0, n, k -> (long) (4 * n) + k + 1),
                "amount", seq(0, n, k -> (long) (50 + 10 * (k % 4)))), false);
        return cat;
    }

    private interface Gen { Object at(int i); }

    private static Object[] seq(int start, int count, Gen g) {
        Object[] a = new Object[count];
        for (int i = 0; i < count; i++) a[i] = g.at(start + i);
        return a;
    }

    private static QueryRequest request(Catalog cat, String table, Filter filter)
            throws Exception {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("table", table);
        if (filter != null) {
            Map<String, Object> fm = new LinkedHashMap<>();
            fm.put("column", filter.column);
            fm.put("op", filter.op);
            fm.put("value", filter.value);
            body.put("filter", fm);
        }
        List<Object> aggs = new ArrayList<>();
        aggs.add(Map.of("alias", "cnt", "func", AggSpec.COUNT));
        aggs.add(Map.of("alias", "cnt_amount", "func", AggSpec.COUNT_COL,
                "column", "amount"));
        aggs.add(Map.of("alias", "total", "func", AggSpec.SUM, "column", "amount"));
        aggs.add(Map.of("alias", "avg_amount", "func", AggSpec.AVG, "column", "amount"));
        aggs.add(Map.of("alias", "min_amount", "func", AggSpec.MIN, "column", "amount"));
        aggs.add(Map.of("alias", "max_amount", "func", AggSpec.MAX, "column", "amount"));
        body.put("aggregates", aggs);
        body.put("returnRows", true);
        body.put("rowLimit", 50);
        return QueryRequest.parse(body, cat);
    }

    private static void testEndToEndAcceptance() throws Exception {
        Path tmp = Files.createTempDirectory("colscan-test-");
        Catalog cat = buildAcceptanceTable(tmp, "demo");

        // EQ 10：命中 shard1 两个边界相等行；NULL 不命中；
        // shard4 统计缺失必须扫描但不命中
        QueryEngine eng = new QueryEngine(cat);
        Map<QueryEngine.Mode, QueryEngine.ModeResult> r =
                eng.execute(request(cat, "demo", new Filter("amount", Filter.EQ, 10L)));
        QueryEngine.ModeResult p = r.get(QueryEngine.Mode.PRUNED);
        QueryEngine.ModeResult f = r.get(QueryEngine.Mode.FULL_SCAN);

        check("EQ10 裁剪模式命中 62 行（两个 NULL 未被当成 0/未命中）",
                p.matchedRows == 62, "matched=" + p.matchedRows);
        check("EQ10 裁剪与全扫描命中行数一致", p.matchedRows == f.matchedRows);
        check("EQ10 裁剪与全扫描聚合完全一致",
                p.aggregates.equals(f.aggregates), p.aggregates + " vs " + f.aggregates);
        check("EQ10 SUM=620（NULL 未参与求和）",
                p.aggregates.get("total").equals(620L),
                String.valueOf(p.aggregates.get("total")));
        check("EQ10 count(*) = 62, count(amount) = 62",
                p.aggregates.get("cnt").equals(62L)
                        && p.aggregates.get("cnt_amount").equals(62L));

        boolean shard4Scanned = p.shards.get(4).scanned
                && p.shards.get(4).reason.equals(Pruner.SCAN_STATS_MISSING);
        check("统计缺失的 shard4 在裁剪模式下仍被强制扫描", shard4Scanned,
                p.shards.get(4).scanned + "/" + p.shards.get(4).reason);
        boolean shard3Pruned = !p.shards.get(3).scanned
                && p.shards.get(3).reason.equals(Pruner.PRUNED_ALL_NULL);
        check("全 NULL 的 shard3 被裁剪", shard3Pruned,
                p.shards.get(3).scanned + "/" + p.shards.get(3).reason);
        boolean shard1Scanned = p.shards.get(1).scanned
                && p.shards.get(1).reason.equals(Pruner.SCAN_RANGE_OVERLAPS);
        check("边界相等的 shard1（min=max=10）被扫描", shard1Scanned);
        check("低值 shard0 与高值 shard2 被裁剪",
                !p.shards.get(0).scanned && !p.shards.get(2).scanned);
        check("裁剪模式扫描 2/5 个分片（shard1 + shard4）",
                p.scannedShards == 2 && p.prunedShards == 3,
                "scanned=" + p.scannedShards);
        check("全扫描模式扫描全部 5 个分片", f.scannedShards == 5);
        check("裁剪读取字节数严格小于全扫描",
                p.totalBytesRead() < f.totalBytesRead(),
                "pruned=" + p.totalBytesRead() + " full=" + f.totalBytesRead());
        check("裁剪模式未读被裁剪分片的任何数据字节",
                p.shards.get(0).dataBytesRead == 0
                        && p.shards.get(2).dataBytesRead == 0
                        && p.shards.get(3).dataBytesRead == 0);
        check("全扫描读取了所有分片的数据字节",
                f.shards.stream().allMatch(s -> s.dataBytesRead > 0));
        check("裁剪模式读取了被扫描分片的 stats.json（字节>0）",
                p.shards.get(1).statsBytesRead > 0);
        check("返回样例行 amount 全为 10 且不含 NULL 行",
                p.rows.size() == 50
                        && p.rows.stream().allMatch(row -> row.get("amount").equals(10L)),
                p.rows.toString());

        // GE 4：shard0 边界相等（max=4）必须扫描且只命中 amount=4 的 16 行
        Map<QueryEngine.Mode, QueryEngine.ModeResult> r2 = eng.execute(
                request(cat, "demo", new Filter("amount", Filter.GE, 4L)));
        QueryEngine.ModeResult p2 = r2.get(QueryEngine.Mode.PRUNED);
        QueryEngine.ModeResult f2 = r2.get(QueryEngine.Mode.FULL_SCAN);
        // 命中：16(shard0) + 62(shard1) + 64(shard2) + 64(shard4) = 206；NULL 共 66 个不计入
        check("GE4 命中 206 行（边界相等行计入，66 个 NULL 不计入）",
                p2.matchedRows == 206, "matched=" + p2.matchedRows);
        check("GE4 裁剪/全扫描一致",
                p2.matchedRows == f2.matchedRows
                        && p2.aggregates.equals(f2.aggregates),
                p2.aggregates + " vs " + f2.aggregates);
        check("GE4 时 shard0 因 max==4 边界相等被扫描且命中 16 行",
                p2.shards.get(0).scanned && p2.shards.get(0).matchedRows == 16);
        check("GE4 统计缺失 shard4 仍被扫描且命中 64 行",
                p2.shards.get(4).scanned && p2.shards.get(4).matchedRows == 64);

        // EQ 0：核心验收——NULL 绝不能当成 0
        Map<QueryEngine.Mode, QueryEngine.ModeResult> r3 = eng.execute(
                request(cat, "demo", new Filter("amount", Filter.EQ, 0L)));
        QueryEngine.ModeResult p3 = r3.get(QueryEngine.Mode.PRUNED);
        QueryEngine.ModeResult f3 = r3.get(QueryEngine.Mode.FULL_SCAN);
        check("EQ0 命中 0 行（全NULL分片没有被当成有 0）",
                p3.matchedRows == 0 && f3.matchedRows == 0,
                "pruned=" + p3.matchedRows + " full=" + f3.matchedRows);
        check("EQ0 裁剪/全扫描聚合一致（SUM/AVG/MIN/MAX 均为 null，不是 0）",
                p3.aggregates.equals(f3.aggregates)
                        && p3.aggregates.get("total") == null
                        && p3.aggregates.get("avg_amount") == null
                        && p3.aggregates.get("min_amount") == null
                        && p3.aggregates.get("max_amount") == null
                        && p3.aggregates.get("cnt").equals(0L)
                        && p3.aggregates.get("cnt_amount").equals(0L),
                p3.aggregates.toString());

        // 无过滤：共 320 行
        Map<QueryEngine.Mode, QueryEngine.ModeResult> r4 =
                eng.execute(request(cat, "demo", null));
        QueryEngine.ModeResult p4 = r4.get(QueryEngine.Mode.PRUNED);
        QueryEngine.ModeResult f4 = r4.get(QueryEngine.Mode.FULL_SCAN);
        check("无过滤时 count(*)=320，count(amount)=254（66 个 NULL 不计入）",
                p4.aggregates.get("cnt").equals(320L)
                        && p4.aggregates.get("cnt_amount").equals(254L),
                p4.aggregates.toString());
        // shard0: (1+2+3+4)*16=160；shard1: 62*10=620；shard2: (100+200+300+400)*16=16000；
        // shard4: (50+60+70+80)*16=4160；合计 20940
        check("无过滤 SUM=20940（NULL 不参与，绝不当成 0）",
                p4.aggregates.get("total").equals(20940L),
                String.valueOf(p4.aggregates.get("total")));
        check("无过滤裁剪/全扫描一致", p4.aggregates.equals(f4.aggregates));
        check("无过滤 AVG 基于 254 个非 NULL 值计算 20940/254≈82.44",
                Math.abs(((Number) p4.aggregates.get("avg_amount")).doubleValue()
                        - 20940.0 / 254.0) < 1e-6,
                String.valueOf(p4.aggregates.get("avg_amount")));
    }

    private static void testNullNeverZeroAggregates() throws Exception {
        // 专门的小表：过滤命中的行上聚合列全是 NULL
        Path tmp = Files.createTempDirectory("colscan-test-");
        Catalog cat = new Catalog(tmp);
        Map<String, String> schema = new LinkedHashMap<>();
        schema.put("k", Types.LONG);
        schema.put("v", Types.LONG);
        cat.appendShard("t", schema, Map.of(
                "k", new Object[]{1L, 1L, 1L},
                "v", new Object[]{null, null, null}), true);
        QueryEngine eng = new QueryEngine(cat);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("table", "t");
        body.put("filter", Map.of("column", "k", "op", "eq", "value", 1L));
        body.put("aggregates", List.of(
                Map.of("alias", "c", "func", AggSpec.COUNT),
                Map.of("alias", "cv", "func", AggSpec.COUNT_COL, "column", "v"),
                Map.of("alias", "s", "func", AggSpec.SUM, "column", "v"),
                Map.of("alias", "a", "func", AggSpec.AVG, "column", "v")));
        QueryRequest req = QueryRequest.parse(body, cat);
        var res = eng.execute(req);
        var p = res.get(QueryEngine.Mode.PRUNED);
        check("命中3行但 v 全 NULL：count(*)=3 且 count(v)=0",
                p.aggregates.get("c").equals(3L) && p.aggregates.get("cv").equals(0L),
                p.aggregates.toString());
        check("全 NULL 输入的 SUM/AVG 返回 null，绝不返回 0",
                p.aggregates.get("s") == null && p.aggregates.get("a") == null,
                p.aggregates.toString());
    }

    private static void testDoubleType() throws Exception {
        Path tmp = Files.createTempDirectory("colscan-test-");
        Catalog cat = new Catalog(tmp);
        Map<String, String> schema = new LinkedHashMap<>();
        schema.put("x", Types.DOUBLE);
        cat.appendShard("d", schema, Map.of("x",
                new Object[]{null, 1.5, 2.25}), true);
        cat.appendShard("d", schema, Map.of("x",
                new Object[]{10.0, 20.0}), true);
        QueryEngine eng = new QueryEngine(cat);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("table", "d");
        body.put("filter", Map.of("column", "x", "op", "ge", "value", 2.25));
        body.put("aggregates", List.of(
                Map.of("alias", "s", "func", AggSpec.SUM, "column", "x")));
        QueryRequest req = QueryRequest.parse(body, cat);
        var res = eng.execute(req);
        var p = res.get(QueryEngine.Mode.PRUNED);
        var f = res.get(QueryEngine.Mode.FULL_SCAN);
        // 命中 2.25,10,20（边界相等 2.25 计入）；NULL 不计入
        check("DOUBLE 边界相等 GE2.25 命中 3 行", p.matchedRows == 3,
                "matched=" + p.matchedRows);
        check("DOUBLE SUM=32.25 且裁剪/全扫描一致",
                ((Number) p.aggregates.get("s")).doubleValue() == 32.25
                        && p.aggregates.equals(f.aggregates),
                p.aggregates.toString());
    }

    private static void testStatsFilePhysicallyMissing() throws Exception {
        // 物理删除 stats.json：必须等价于统计缺失，强制扫描
        Path tmp = Files.createTempDirectory("colscan-test-");
        Catalog cat = buildAcceptanceTable(tmp, "demo2");
        Files.deleteIfExists(cat.statsFile("demo2", 2));
        QueryEngine eng = new QueryEngine(cat);
        var res = eng.execute(request(cat, "demo2",
                new Filter("amount", Filter.EQ, 10L)));
        var p = res.get(QueryEngine.Mode.PRUNED);
        check("stats.json 物理缺失的 shard2 被强制扫描",
                p.shards.get(2).scanned
                        && p.shards.get(2).reason.equals(Pruner.SCAN_STATS_MISSING),
                p.shards.get(2).scanned + "/" + p.shards.get(2).reason);
        var f = res.get(QueryEngine.Mode.FULL_SCAN);
        check("物理缺失统计下结果仍与全扫描一致",
                p.matchedRows == f.matchedRows
                        && p.aggregates.equals(f.aggregates));
    }

    // ---------------- HTTP 端到端 ----------------

    private static void testHttpEndToEnd() throws Exception {
        Path tmp = Files.createTempDirectory("colscan-test-");
        // 在同 JVM 内启动服务（绑定随机端口）
        Catalog cat = new Catalog(tmp);
        com.sun.net.httpserver.HttpServer server =
                colscan.server.HttpApi.newServer("127.0.0.1", 0, cat);
        int port = server.getAddress().getPort();
        String base = "http://127.0.0.1:" + port;
        HttpClient http = HttpClient.newHttpClient();

        try {
            // health
            HttpResponse<String> h = http.send(HttpRequest.newBuilder(URI.create(base + "/health"))
                    .GET().build(), HttpResponse.BodyHandlers.ofString());
            check("HTTP /health 200", h.statusCode() == 200
                    && h.body().contains("\"ok\""), h.body());

            // 三个分片（每片 40 行）：普通、全NULL、统计缺失
            httpPost(http, base + "/tables/t/ingest",
                    ingestBody(false, 128, i -> i < 126 ? 1L : 10L));
            httpPost(http, base + "/tables/t/ingest",
                    ingestBody(false, 128, i -> null));
            httpPost(http, base + "/tables/t/ingest",
                    ingestBody(true, 128, i -> 100L + (i % 2) * 100L));

            // GET /tables
            HttpResponse<String> tl = http.send(HttpRequest.newBuilder(
                    URI.create(base + "/tables")).GET().build(),
                    HttpResponse.BodyHandlers.ofString());
            Map<String, Object> tablesResp = Json.parseObject(tl.body());
            List<?> tableList = (List<?>) tablesResp.get("tables");
            long httpShardCount = tableList.isEmpty() ? -1
                    : ((Number) ((Map<?, ?>) tableList.get(0)).get("shardCount")).longValue();
            check("HTTP GET /tables 显示 3 个分片",
                    tl.statusCode() == 200 && httpShardCount == 3, tl.body());

            // 查询 EQ 10：只命中第一片 2 行；第二片全NULL裁剪；第三片统计缺失扫描。
            // returnRows=true 使投影列 id 也成为必需列，跳片节省的数据量大于统计读取量
            String queryBody = "{\"table\":\"t\",\"filter\":{\"column\":\"v\",\"op\":\"eq\","
                    + "\"value\":10},\"returnRows\":true,\"rowLimit\":10,"
                    + "\"aggregates\":[{\"alias\":\"c\",\"func\":\"count\"},"
                    + "{\"alias\":\"s\",\"func\":\"sum\",\"column\":\"v\"}]}";
            HttpResponse<String> q = httpPost(http, base + "/query", queryBody);
            Map<String, Object> qm = Json.parseObject(q.body());
            check("HTTP 查询 consistentWithFullScan=true",
                    Boolean.TRUE.equals(qm.get("consistentWithFullScan")), q.body());
            Map<?, ?> pruned = (Map<?, ?>) qm.get("pruned");
            Map<?, ?> full = (Map<?, ?>) qm.get("fullScan");
            check("HTTP 裁剪命中 2 行，全扫描同样命中 2 行",
                    ((Number) pruned.get("matchedRows")).longValue() == 2
                            && ((Number) full.get("matchedRows")).longValue() == 2,
                    q.body());
            check("HTTP 裁剪读取字节 < 全扫描（分片足够大时跳过分片有净收益）",
                    ((Number) pruned.get("totalBytesRead")).longValue()
                            < ((Number) full.get("totalBytesRead")).longValue(),
                    "pruned=" + pruned.get("totalBytesRead")
                            + " full=" + full.get("totalBytesRead"));
            check("HTTP 裁剪扫描 2/3 分片（边界相等片 + 统计缺失片）",
                    ((Number) pruned.get("scannedShards")).longValue() == 2
                            && ((Number) pruned.get("prunedShards")).longValue() == 1,
                    pruned.toString());

            // 错误请求校验
            HttpResponse<String> bad = http.send(HttpRequest.newBuilder(
                    URI.create(base + "/query"))
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString("{\"table\":\"nope\"}"))
                    .build(), HttpResponse.BodyHandlers.ofString());
            check("HTTP 查询不存在的表返回 400", bad.statusCode() == 400
                    && bad.body().contains("表不存在"), bad.body());
        } finally {
            server.stop(0);
        }
    }

    private interface VGen { Long at(int i); }

    private static String ingestBody(boolean noStats, int n, VGen vf) {
        StringBuilder sb = new StringBuilder();
        sb.append("{\"types\":{\"id\":\"LONG\",\"v\":\"LONG\"}");
        if (noStats) sb.append(",\"computeStats\":false");
        sb.append(",\"rows\":[");
        for (int i = 0; i < n; i++) {
            if (i > 0) sb.append(',');
            Long v = vf.at(i);
            sb.append("{\"id\":").append(i + 1).append(",\"v\":")
                    .append(v == null ? "null" : v).append('}');
        }
        sb.append("]}");
        return sb.toString();
    }

    private static HttpResponse<String> httpPost(HttpClient http, String url, String body)
            throws IOException, InterruptedException {
        HttpResponse<String> resp = http.send(HttpRequest.newBuilder(URI.create(url))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build(), HttpResponse.BodyHandlers.ofString());
        if (resp.statusCode() != 200) {
            throw new AssertionError("POST " + url + " -> " + resp.statusCode()
                    + ": " + resp.body());
        }
        return resp;
    }
}
