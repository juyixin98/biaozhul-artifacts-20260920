package intervaljoin;

import java.util.ArrayList;
import java.util.List;
import java.util.Set;

/**
 * 入口。
 *
 *   java intervaljoin.Main server [port]   启动 HTTP 服务（默认 8080）
 *   java intervaljoin.Main demo [hotN]     内置验收演练：三种场景 + 离线全量比对
 *
 * demo 退出码：0 全部通过，1 存在断言失败。
 */
public final class Main {

    /** 最小化断言器：统计通过/失败，失败项留痕。 */
    static final class Checker {
        int passed, failed;
        final List<String> failures = new ArrayList<>();

        void check(String name, boolean cond) {
            if (cond) {
                passed++;
                System.out.println("  [PASS] " + name);
            } else {
                failed++;
                failures.add(name);
                System.out.println("  [FAIL] " + name);
            }
        }

        boolean ok() { return failed == 0; }
    }

    public static void main(String[] args) throws Exception {
        String mode = args.length > 0 ? args[0] : "server";
        switch (mode) {
            case "server": {
                int port = args.length > 1 ? Integer.parseInt(args[1]) : 8080;
                JoinEngine engine = new JoinEngine(0, 0);
                ApiServer api = new ApiServer(engine);
                api.start(port);
                System.out.println("Interval join HTTP server started on port " + api.getAddressPort());
                System.out.println("Default window: lowerBound=0, upperBound=0 (POST /config to change)");
                Thread.currentThread().join();
                break;
            }
            case "demo": {
                int hotN = args.length > 1 ? Integer.parseInt(args[1]) : 20000;
                Checker c = new Checker();
                runDemo(c, hotN);
                System.out.println("\n断言汇总：" + c.passed + " passed, " + c.failed + " failed");
                boolean ok = c.ok();
                System.out.println(ok ? "DEMO RESULT: PASS" : "DEMO RESULT: FAIL");
                System.exit(ok ? 0 : 1);
                break;
            }
            default:
                System.err.println("usage: java intervaljoin.Main [server [port] | demo [hotN]]");
                System.exit(2);
        }
    }

    static void runDemo(Checker c, int hotN) {
        scenarioUnevenWatermarks(c);
        scenarioBoundaries(c);
        scenarioHotKey(c, hotN);
    }

    /** 场景一：两侧水位推进不均 + 单侧推进不回收。 */
    static void scenarioUnevenWatermarks(Checker c) {
        System.out.println("=== 场景1：两侧水位进度不均（窗口 [-2, 3]）===");
        JoinEngine eng = new JoinEngine(-2, 3);
        List<Event> all = new ArrayList<>();

        Event l1 = new Event("left", "a", 10, "L1");
        all.add(l1);
        List<Pair> p = eng.ingest(l1);
        c.check("初始无配对", p.isEmpty());

        long purged = eng.advanceLeftWatermark(20);
        c.check("单侧推进水位回收量=0", purged == 0);
        c.check("单侧推进后左事件仍驻留", eng.retainedEvents() == 1);

        Event r1 = new Event("right", "a", 8, "R1");
        all.add(r1);
        p = eng.ingest(r1);
        c.check("落后右流事件仍能命中（证明单侧不回收的必要性）",
                p.size() == 1 && p.get(0).left == l1);

        Event r2 = new Event("right", "a", 14, "R2");
        all.add(r2);
        p = eng.ingest(r2);
        c.check("超界事件不配对", p.isEmpty());

        purged = eng.advanceRightWatermark(12);
        c.check("有效水位12：仅 R1 被回收", purged == 1 && eng.getStats().rightPurged == 1);
        c.check("L1@10 仍保留（未来右事件9..13可能命中）", eng.retainedEvents() == 2);

        purged = eng.advanceRightWatermark(24);
        c.check("有效水位20：L1、R2 回收", purged == 2 && eng.retainedEvents() == 0);
        c.check("空 key 槽被移除", eng.retainedKeys() == 0);

        Event late = new Event("left", "a", 19, "LATE");
        p = eng.ingest(late);
        c.check("严格迟到事件被丢弃", p.isEmpty() && eng.getStats().leftLateDropped == 1);

        Event onEdge = new Event("left", "b", 20, "EDGE");
        all.add(onEdge);
        p = eng.ingest(onEdge);
        c.check("ts==watermark 不算迟到（leftAccepted=2: L1、EDGE）",
                eng.getStats().leftAccepted == 2
                        && eng.getStats().leftLateDropped == 1);

        System.out.println("  状态：" + stateLine(eng));
    }

    /** 场景二：时间边界闭区间逐格验证，且与离线结果一致。 */
    static void scenarioBoundaries(Checker c) {
        System.out.println("=== 场景2：时间边界（窗口 [-2, 3]，delta 从 -4 到 5）===");
        JoinEngine eng = new JoinEngine(-2, 3);
        List<Event> all = new ArrayList<>();
        for (long d = -4; d <= 5; d++) {
            String k = "k" + d;
            Event l = new Event("left", k, 10, "L" + d);
            Event r = new Event("right", k, 10 + d, "R" + d);
            all.add(l);
            all.add(r);
            eng.ingest(r);
            eng.ingest(l);
        }
        Set<String> got = DataGen.toIdSet(collectPairs(all, new JoinEngine(-2, 3)));
        Set<String> want = DataGen.toIdSet(OfflineJoin.nestedLoop(all, -2, 3));
        c.check("流式配对集合 == 离线全量连接", got.equals(want));
        c.check("边界内配对数=6（d=-2,-1,0,1,2,3）", want.size() == 6);

        JoinEngine eng2 = new JoinEngine(0, 0);
        List<Event> g2 = new ArrayList<>();
        for (int i = 0; i < 3; i++) g2.add(new Event("left", "h", 5, "LA" + i));
        for (int i = 0; i < 2; i++) g2.add(new Event("right", "h", 5, "RB" + i));
        List<Pair> out = new ArrayList<>();
        for (Event e : g2) out.addAll(eng2.ingest(e));
        c.check("同刻 3x2 事件产生 6 对", out.size() == 6);
        c.check("无重复配对（Set 去重后仍为 6）", DataGen.toIdSet(out).size() == 6);

        System.out.println("  状态：" + stateLine(eng));
    }

    /** 场景三：极端热键 + 冷键混合大数据，与离线全量连接比对，输出回收量。 */
    static void scenarioHotKey(Checker c, int hotN) {
        long lower = -2, upper = 3;
        System.out.println("=== 场景3：极端热键（每侧 " + hotN
                + " 条同键）+ 冷键边界，窗口 [-2,3] ===");
        DataGen.Dataset ds = DataGen.generate(hotN, 500, lower, upper, 42);

        long t0 = System.nanoTime();
        JoinEngine eng = new JoinEngine(lower, upper);
        List<Pair> streamed = new ArrayList<>();
        for (Event e : ds.events) {
            streamed.addAll(eng.ingest(e));
        }
        long ingestMs = (System.nanoTime() - t0) / 1_000_000;

        long t1 = System.nanoTime();
        List<Pair> offlineSort = OfflineJoin.sortMerge(ds.events, lower, upper);
        List<Pair> offlineNested = OfflineJoin.nestedLoop(ds.events, lower, upper);
        long offlineMs = (System.nanoTime() - t1) / 1_000_000;

        Set<String> got = DataGen.toIdSet(streamed);
        Set<String> want = DataGen.toIdSet(offlineSort);
        c.check("两个离线版本自身一致",
                want.equals(DataGen.toIdSet(offlineNested)));
        c.check("流配对数 " + streamed.size() + " == 离线 " + offlineSort.size(),
                streamed.size() == offlineSort.size());
        c.check("流式配对集合 == 离线全量连接（热键+冷键）", got.equals(want));

        long retainedBefore = eng.retainedEvents();
        long purgedL = eng.advanceLeftWatermark(Long.MAX_VALUE / 2);
        c.check("仅左水位推到极大：有效水位仍为 -∞，回收 0", purgedL == 0
                && eng.retainedEvents() == retainedBefore);
        long purgedR = eng.advanceRightWatermark(Long.MAX_VALUE / 2);
        c.check("右水位也推到极大：剩余状态全部回收",
                eng.retainedEvents() == 0 && eng.retainedKeys() == 0);
        c.check("末尾大批量回收数 = 此前驻留数",
                purgedL + purgedR == retainedBefore);

        Stats s = eng.getStats();
        System.out.println("  事件总数=" + ds.events.size()
                + " 配对总数=" + offlineSort.size()
                + " 流式耗时=" + ingestMs + "ms 离线耗时=" + offlineMs + "ms");
        System.out.println("  回收左事件=" + s.leftPurged
                + " 回收右事件=" + s.rightPurged
                + " 移除左key槽=" + s.leftKeySlotsRemoved
                + " 移除右key槽=" + s.rightKeySlotsRemoved
                + " 迟到丢弃 L/R=" + s.leftLateDropped + "/" + s.rightLateDropped);
        System.out.println("  收尾前驻留事件=" + retainedBefore);
    }

    private static List<Pair> collectPairs(List<Event> events, JoinEngine eng) {
        List<Pair> all = new ArrayList<>();
        for (Event e : events) all.addAll(eng.ingest(e));
        return all;
    }

    private static String stateLine(JoinEngine eng) {
        Stats s = eng.getStats();
        return "pairs=" + s.pairsEmitted
                + " purged(L/R)=" + s.leftPurged + "/" + s.rightPurged
                + " retained=" + eng.retainedEvents()
                + " late(L/R)=" + s.leftLateDropped + "/" + s.rightLateDropped;
    }
}
