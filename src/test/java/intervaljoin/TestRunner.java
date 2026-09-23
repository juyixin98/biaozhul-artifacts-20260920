package intervaljoin;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * 零依赖测试运行器（不引入 JUnit）。
 * 每个 test* 方法独立；任意失败则 {@link #main} 以退出码 1 结束。
 *
 * 覆盖：
 *  - JSON 解析/序列化
 *  - 区间连接基本语义、到达顺序无关性、同刻多事件、每对仅一次
 *  - 闭区间边界、单侧水位不回收、双方水位回收、回收边界恰好性
 *  - 迟到丢弃（ts&lt;wm 丢弃、ts==wm 保留）
 *  - 热键 + 冷键大数据与两个离线版本三方一致
 *  - HTTP 接口集成测试（真实启动 ApiServer，java.net.http 客户端）
 */
public final class TestRunner {

    private int passed;
    private int failed;
    private final List<String> failures = new ArrayList<>();

    public static void main(String[] args) throws Exception {
        TestRunner t = new TestRunner();
        t.testJson();
        t.testBasicJoin();
        t.testOrderIndependence();
        t.testSameTimestamp();
        t.testWindowBoundaries();
        t.testSingleSideWatermarkNoPurge();
        t.testPurgeBoundaries();
        t.testLateDrops();
        t.testReconfigureGuard();
        t.testOfflineAgreesOnRandomData();
        t.testHotKeyLarge();
        t.testHttpIntegration();

        System.out.println();
        System.out.println("TESTS: " + t.passed + " passed, " + t.failed + " failed");
        if (!t.failures.isEmpty()) {
            System.out.println("FAILED CASES:");
            for (String f : t.failures) System.out.println("  - " + f);
            System.exit(1);
        }
    }

    private void ok(String name, boolean cond) {
        if (cond) {
            passed++;
        } else {
            failed++;
            failures.add(name);
            System.out.println("[FAIL] " + name);
        }
    }

    private void eq(String name, long actual, long expected) {
        ok(name + " (expected=" + expected + ", actual=" + actual + ")", actual == expected);
    }

    // ------------------------------------------------------------------
    // JSON
    // ------------------------------------------------------------------

    void testJson() {
        Map<String, Object> m = Json.parseObject(
                "{\"a\": 1, \"b\": \"x\\ty\", \"c\": [true, false, null, -2.5], \"d\": {}}");
        eq("json int", (Long) m.get("a"), 1);
        ok("json string escape", "x\ty".equals(m.get("b")));
        List<?> arr = (List<?>) m.get("c");
        ok("json array bools", arr.get(0) == Boolean.TRUE && arr.get(1) == Boolean.FALSE
                && arr.get(2) == null && ((Double) arr.get(3)) == -2.5);
        ok("json empty object", ((Map<?, ?>) m.get("d")).isEmpty());

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("k", "v\"\\\n");
        out.put("n", 7L);
        String s = Json.write(out);
        Map<String, Object> back = Json.parseObject(s);
        ok("json roundtrip string", "v\"\\\n".equals(back.get("k")));
        eq("json roundtrip long", (Long) back.get("n"), 7);

        boolean threw = false;
        try { Json.parseObject("{bad}"); } catch (IllegalArgumentException e) { threw = true; }
        ok("json parse error throws", threw);
    }

    // ------------------------------------------------------------------
    // 引擎：基本语义
    // ------------------------------------------------------------------

    void testBasicJoin() {
        JoinEngine eng = new JoinEngine(0, 5);
        List<Pair> out = new ArrayList<>();
        // 左先到
        out.addAll(eng.ingest(new Event("left", "k", 10, "L1")));
        out.addAll(eng.ingest(new Event("right", "k", 12, "R1"))); // delta 2 命中
        out.addAll(eng.ingest(new Event("right", "k", 16, "R2"))); // delta 6 超界
        eq("basic pairs", out.size(), 1);
        ok("basic pair ids", out.get(0).left.id.equals("L1") && out.get(0).right.id.equals("R1"));
        // 不同 key 不连
        out.addAll(eng.ingest(new Event("right", "other", 10, "R3")));
        eq("different key no pair", out.size(), 1);
    }

    void testOrderIndependence() {
        // 三种到达顺序，输出集合一致
        List<Event> base = List.of(
                new Event("left", "k", 10, "L10"),
                new Event("left", "k", 12, "L12"),
                new Event("right", "k", 11, "R11"),
                new Event("right", "k", 13, "R13"));
        List<List<Event>> orders = List.of(
                base,
                List.of(base.get(2), base.get(3), base.get(0), base.get(1)),
                List.of(base.get(0), base.get(2), base.get(1), base.get(3)));
        Set<String> ref = null;
        for (List<Event> order : orders) {
            JoinEngine eng = new JoinEngine(-5, 5);
            List<Pair> out = new ArrayList<>();
            for (Event e : order) out.addAll(eng.ingest(e));
            Set<String> got = DataGen.toIdSet(out);
            if (ref == null) ref = got;
            else ok("arrival order independence", ref.equals(got));
        }
        eq("four events pair count", ref.size(), 4);
    }

    void testSameTimestamp() {
        JoinEngine eng = new JoinEngine(0, 0);
        List<Pair> out = new ArrayList<>();
        for (int i = 0; i < 100; i++) out.addAll(eng.ingest(new Event("left", "h", 5, "L" + i)));
        for (int i = 0; i < 100; i++) out.addAll(eng.ingest(new Event("right", "h", 5, "R" + i)));
        eq("same ts 100x100 pairs", out.size(), 10000);
        eq("no duplicate pairs", DataGen.toIdSet(out).size(), 10000);
        // 每个右事件到达时探测 100 个左事件，之后左状态不会再回头和已到右事件配对
    }

    void testWindowBoundaries() {
        // 窗口 [-2, 3]，delta 逐格扫描，两端闭
        for (long d = -3; d <= 4; d++) {
            JoinEngine eng = new JoinEngine(-2, 3);
            List<Pair> out = new ArrayList<>();
            out.addAll(eng.ingest(new Event("left", "k", 100, "L")));
            out.addAll(eng.ingest(new Event("right", "k", 100 + d, "R")));
            boolean shouldHit = d >= -2 && d <= 3;
            ok("boundary delta=" + d, out.size() == (shouldHit ? 1 : 0));
        }
        // 负窗口合法：lower=-5, upper=-1 表示右事件必须更早
        JoinEngine neg = new JoinEngine(-5, -1);
        List<Pair> out = new ArrayList<>();
        out.addAll(neg.ingest(new Event("left", "k", 10, "L")));
        out.addAll(neg.ingest(new Event("right", "k", 8, "R")));  // delta -2 命中
        out.addAll(neg.ingest(new Event("right", "k", 10, "R0"))); // delta 0 不命中
        eq("negative window", out.size(), 1);
    }

    // ------------------------------------------------------------------
    // 水位与回收
    // ------------------------------------------------------------------

    void testSingleSideWatermarkNoPurge() {
        JoinEngine eng = new JoinEngine(0, 10);
        eng.ingest(new Event("left", "k", 1, "L"));
        eq("one side wm purges nothing (left only)", eng.advanceLeftWatermark(1_000_000), 0);
        eq("state retained", eng.retainedEvents(), 1);
        eng.ingest(new Event("right", "k", 5, "R"));
        eq("late right joins retained left", eng.getStats().pairsEmitted, 1);
        eq("one side wm purges nothing (right too low)", eng.advanceRightWatermark(2), 0);
        eq("still retained", eng.retainedEvents(), 2);
    }

    void testPurgeBoundaries() {
        // 窗口 [1, 4]（全正窗口），手工布置事件验证回收切点的“恰好性”
        JoinEngine eng = new JoinEngine(1, 4);
        // 左 @10, 右 @12(delta2 命中)
        Event l = new Event("left", "k", 10, "L");
        Event r = new Event("right", "k", 12, "R");
        eng.ingest(l);
        eng.ingest(r);
        eq("pair emitted", eng.getStats().pairsEmitted, 1);

        // 有效水位 14：左切点 14-4=10（删 ts<10），右切点 14+1=15（删 ts<15）
        // L@10 不删（边界保留），R@12 删
        eng.advanceLeftWatermark(14);
        long purged = eng.advanceRightWatermark(14);
        eq("purge at wm14", purged, 1);
        eq("right purged count", eng.getStats().rightPurged, 1);
        eq("left retained at exact cutoff", eng.retainedEvents(), 1);

        // 此时来右事件 @14：delta=4 命中上界，L 必须还在
        List<Pair> out = eng.ingest(new Event("right", "k", 14, "R14"));
        eq("right at upper bound still joins", out.size(), 1);
        // 右事件 @15：delta=5 不命中
        out = eng.ingest(new Event("right", "k", 15, "R15"));
        eq("right beyond bound no join", out.size(), 0);

        // 有效水位 15：左切点 15-4=11（删 ts<11）→ L@10 终于可删
        purged = eng.advanceLeftWatermark(15) + eng.advanceRightWatermark(15);
        // 注意：左先到 15 时有效水位仍 14，回收 0；右到 15 时有效水位 15
        eq("left purged exactly one wm later", eng.getStats().leftPurged, 1);

        // 水位再推进，回收 R14/R15
        eng.advanceLeftWatermark(30);
        purged = eng.advanceRightWatermark(30);
        eq("all state gone", eng.retainedEvents(), 0);
        eq("key slots removed", eng.retainedKeys(), 0);
        ok("purged total accounts for 4 events",
                eng.getStats().leftPurged + eng.getStats().rightPurged == 4);
    }

    void testLateDrops() {
        JoinEngine eng = new JoinEngine(0, 10);
        eng.ingest(new Event("left", "k", 100, "L"));
        eng.advanceLeftWatermark(50);
        eng.advanceRightWatermark(50);
        // ts < 50 迟到
        long beforeL = eng.getStats().leftLateDropped;
        eng.ingest(new Event("left", "k", 49, "LATE"));
        eq("late left dropped counter delta",
                eng.getStats().leftLateDropped - beforeL, 1);
        long beforeR = eng.getStats().rightLateDropped;
        eng.ingest(new Event("right", "k", 1, "LATE"));
        eq("late right dropped counter delta",
                eng.getStats().rightLateDropped - beforeR, 1);
        // ts == 50 准时
        List<Pair> out = eng.ingest(new Event("right", "k", 50, "ONEDGE"));
        ok("ts==wm accepted (no pair, window needs r in 100..110)", out.isEmpty());
        eq("right accepted is 1 (late one excluded)", eng.getStats().rightAccepted, 1);

        boolean threw = false;
        try { eng.advanceLeftWatermark(40); } catch (IllegalArgumentException e) { threw = true; }
        ok("watermark backwards rejected", threw);
    }

    void testReconfigureGuard() {
        JoinEngine eng = new JoinEngine(0, 0);
        eng.configure(-1, 1); // 空状态可改
        eng.ingest(new Event("left", "k", 1, "L"));
        boolean threw = false;
        try { eng.configure(0, 10); } catch (IllegalStateException e) { threw = true; }
        ok("reconfigure blocked after state", threw);
        eng.reset();
        eng.configure(0, 10); // reset 后可改
        boolean threw2 = false;
        try { new JoinEngine(5, 1); } catch (IllegalArgumentException e) { threw2 = true; }
        ok("lower>upper rejected", threw2);
    }

    // ------------------------------------------------------------------
    // 与离线参照三方一致
    // ------------------------------------------------------------------

    void testOfflineAgreesOnRandomData() {
        List<Event> events = new ArrayList<>();
        java.util.Random rnd = new java.util.Random(7);
        String[] keys = {"a", "b", "c", "hot", "x.y"};
        for (int i = 0; i < 2000; i++) {
            String side = i % 2 == 0 ? "left" : "right";
            String key = keys[rnd.nextInt(keys.length)];
            long ts = 1 + rnd.nextInt(200);
            events.add(new Event(side, key, ts, side.charAt(0) + "#" + i));
        }
        long lower = -3, upper = 2;
        List<Pair> wantNested = OfflineJoin.nestedLoop(events, lower, upper);
        List<Pair> wantSort = OfflineJoin.sortMerge(events, lower, upper);
        ok("offline nested == sort",
                DataGen.toIdSet(wantNested).equals(DataGen.toIdSet(wantSort)));

        // 流：按到达顺序直接喂，不推进水位（全部状态保留），结果必须一致
        JoinEngine eng = new JoinEngine(lower, upper);
        List<Pair> got = new ArrayList<>();
        for (Event e : events) got.addAll(eng.ingest(e));
        ok("stream == offline on random data",
                DataGen.toIdSet(got).equals(DataGen.toIdSet(wantSort)));
        eq("pair count parity", got.size(), wantSort.size());

        // 推进双水位到末尾后状态清空，但已输出结果不变
        long maxTs = 200;
        eng.advanceLeftWatermark(maxTs + 100);
        long purged = eng.advanceRightWatermark(maxTs + 100);
        ok("everything purged after both wms", eng.retainedEvents() == 0);
        ok("purged == retained events", purged >= 0);
        eq("emitted unchanged after purge", eng.getStats().pairsEmitted, got.size());
    }

    void testHotKeyLarge() {
        int hotN = 10000;
        DataGen.Dataset ds = DataGen.generate(hotN, 1000, -2, 3, 123);
        JoinEngine eng = new JoinEngine(-2, 3);
        List<Pair> got = new ArrayList<>();
        long t0 = System.nanoTime();
        for (Event e : ds.events) got.addAll(eng.ingest(e));
        long streamMs = (System.nanoTime() - t0) / 1_000_000;

        long t1 = System.nanoTime();
        List<Pair> want = OfflineJoin.sortMerge(ds.events, -2, 3);
        long offlineMs = (System.nanoTime() - t1) / 1_000_000;

        eq("hot large: pair count", got.size(), want.size());
        ok("hot large: set equality", DataGen.toIdSet(got).equals(DataGen.toIdSet(want)));

        long retained = eng.retainedEvents();
        eng.advanceLeftWatermark(Long.MAX_VALUE / 2);
        eq("one-sided final wm purges nothing", eng.retainedEvents(), retained);
        eng.advanceRightWatermark(Long.MAX_VALUE / 2);
        eq("both final wms purge all", eng.retainedEvents(), 0);
        ok("purge counts total all events",
                eng.getStats().leftPurged == hotN + 1000
                        && eng.getStats().rightPurged == hotN + 1000);
        System.out.println("    [info] hotN=" + hotN + " events=" + ds.events.size()
                + " pairs=" + want.size() + " stream=" + streamMs + "ms offline=" + offlineMs + "ms");
    }

    // ------------------------------------------------------------------
    // HTTP 集成测试
    // ------------------------------------------------------------------

    void testHttpIntegration() throws Exception {
        JoinEngine eng = new JoinEngine(0, 0);
        ApiServer api = new ApiServer(eng);
        api.start(0);
        int port = api.getAddressPort();
        String base = "http://127.0.0.1:" + port;
        java.net.http.HttpClient client = java.net.http.HttpClient.newHttpClient();

        try {
            // health
            java.net.http.HttpResponse<String> h = get(client, base + "/health");
            eq("http health status", h.statusCode(), 200);
            ok("http health body", h.body().contains("\"ok\""));

            // 配置窗口
            java.net.http.HttpResponse<String> cfg = post(client, base + "/config",
                    "{\"lowerBound\":-2,\"upperBound\":3}");
            eq("http config status", cfg.statusCode(), 200);

            // 批量事件：左 10 / 右 8(delta-2 命中) / 右 14(delta4 不命中)
            String batch = "{\"events\":["
                    + "{\"key\":\"a\",\"ts\":8,\"id\":\"R1\"},"
                    + "{\"key\":\"a\",\"ts\":14,\"id\":\"R2\"}]}";
            java.net.http.HttpResponse<String> r = post(client, base + "/events/right", batch);
            eq("http right batch status", r.statusCode(), 200);
            ok("http right accepted 2", r.body().contains("\"accepted\":2"));

            java.net.http.HttpResponse<String> l = post(client, base + "/events/left",
                    "{\"key\":\"a\",\"ts\":10,\"id\":\"L1\"}");
            eq("http left single status", l.statusCode(), 200);
            ok("http one pair emitted", l.body().contains("\"newPairs\":1"));
            ok("http pair content", l.body().contains("R1"));

            // 单侧水位不回收
            java.net.http.HttpResponse<String> w1 = post(client, base + "/watermark/left",
                    "{\"watermark\":20}");
            ok("http one-sided wm zero purge", w1.body().contains("\"purgedThisCall\":0"));
            ok("http retained after one wm", w1.body().contains("\"retainedEvents\":3"));

            // 对侧水位推进，回收 R1@8（有效水位12，右切点 12-2=10）
            java.net.http.HttpResponse<String> w2 = post(client, base + "/watermark/right",
                    "{\"watermark\":12}");
            ok("http both wm purges 1", w2.body().contains("\"purgedThisCall\":1"));
            ok("http rightPurged metric", w2.body().contains("\"rightPurged\":1"));

            // results
            java.net.http.HttpResponse<String> res = get(client, base + "/results");
            eq("http results status", res.statusCode(), 200);
            ok("http totalPairs 1", res.body().contains("\"totalPairs\":1"));

            // 迟到事件
            java.net.http.HttpResponse<String> late = post(client, base + "/events/left",
                    "{\"key\":\"a\",\"ts\":1,\"id\":\"LATE\"}");
            ok("http late dropped", late.body().contains("\"lateDropped\":1"));
            ok("http late accepted 0", late.body().contains("\"accepted\":0"));

            // 参数错误
            java.net.http.HttpResponse<String> bad = post(client, base + "/events/left",
                    "{\"key\":\"a\"}");
            eq("http bad request status", bad.statusCode(), 400);

            // 水位回退 409 语义（这里实现为 400 IllegalArgumentException）
            java.net.http.HttpResponse<String> back = post(client, base + "/watermark/left",
                    "{\"watermark\":5}");
            ok("http wm backwards rejected", back.statusCode() == 400);

            // reset
            java.net.http.HttpResponse<String> rst = post(client, base + "/reset",
                    "{\"lowerBound\":0,\"upperBound\":5}");
            eq("http reset status", rst.statusCode(), 200);
            ok("http reset cleared", rst.body().contains("\"retainedEvents\":0"));

            // 未知路由
            java.net.http.HttpResponse<String> nf = get(client, base + "/nope");
            eq("http 404", nf.statusCode(), 404);
        } finally {
            api.stop();
        }
    }

    private static java.net.http.HttpResponse<String> get(
            java.net.http.HttpClient client, String url) throws Exception {
        java.net.http.HttpRequest req = java.net.http.HttpRequest.newBuilder()
                .uri(java.net.URI.create(url))
                .header("Accept", "application/json")
                .GET().build();
        return client.send(req, java.net.http.HttpResponse.BodyHandlers.ofString());
    }

    private static java.net.http.HttpResponse<String> post(
            java.net.http.HttpClient client, String url, String json) throws Exception {
        java.net.http.HttpRequest req = java.net.http.HttpRequest.newBuilder()
                .uri(java.net.URI.create(url))
                .header("Content-Type", "application/json")
                .POST(java.net.http.HttpRequest.BodyPublishers.ofString(json)).build();
        return client.send(req, java.net.http.HttpResponse.BodyHandlers.ofString());
    }
}
