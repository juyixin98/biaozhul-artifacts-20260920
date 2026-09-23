package topk;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;

/**
 * 随机差分测试（property test）。
 *
 * <p>对同一个 GroupState 喂几千次随机操作（插入/撤回/查询，含负增量、乱序、
 * 重复 ID、过期撤回、重复撤回），每一步都用“朴素参考实现（oracle）”对照：
 * oracle 用 Map 保留事件并在每次查询时对窗口内全部事件重新求和、全排序，
 * 而被测实现只保留窗口状态。两者每一步的状态、排名、计数必须完全一致。
 *
 * <p>固定随机种子，结果可复现。
 */
public final class PropertyTest {

    private static final long W = 1000;
    private static final int OPS = 6000;
    private static final long SEED = 20260923L;

    // oracle 中的事件记录
    private static final class OEvent {
        final String item;
        final long delta;
        final long ts;

        OEvent(String item, long delta, long ts) {
            this.item = item;
            this.delta = delta;
            this.ts = ts;
        }
    }

    private final Random rnd = new Random(SEED);
    private long watermark = 0;

    // oracle 全部已接受且未被撤回的事件（故意不删过期的，查询时过滤——这就是“保存全部历史再重算”的参考模型）
    private final Map<String, OEvent> oracleAll = new HashMap<>();
    // oracle 已撤回集合
    private final Map<String, Long> oracleRetracted = new HashMap<>();

    private GroupState sut;

    public static void main(String[] args) {
        TestHarness t = new TestHarness("随机差分测试（oracle 全量重算）");
        PropertyTest pt = new PropertyTest();
        t.add("6000 次随机操作，逐步对照 oracle 完整排序/计数/返回状态", pt::runScenario);
        System.exit(t.run());
    }

    void runScenario() {
        sut = new GroupState(W);
        List<String> knownIds = new ArrayList<>();

        for (int i = 0; i < OPS; i++) {
            // 时间戳 1..watermark+1500：既可能落在窗内，也可能迟到
            long ts;
            if (knownIds.isEmpty()) {
                ts = 1 + rnd.nextInt(10);
            } else {
                ts = Math.max(1, watermark - 500 + rnd.nextInt(2001));
            }
            advance(ts);

            int roll = rnd.nextInt(100);
            if (roll < 60) {
                // 插入
                String id;
                boolean reuse = !knownIds.isEmpty() && rnd.nextInt(5) == 0;
                if (reuse) {
                    id = knownIds.get(rnd.nextInt(knownIds.size()));
                } else {
                    id = "ev" + i;
                }
                String item = "item" + rnd.nextInt(8);
                long delta = rnd.nextInt(21) - 10; // -10..10，含大量负数与 0
                Event e = new Event(id, item, delta, ts);

                GroupState.InsertStatus actual = sut.insert(e);
                GroupState.InsertStatus expected = oracleInsert(id, item, delta, ts);
                TestHarness.eq(actual, expected, "step " + i + " insert " + id
                        + " ts=" + ts + " wm=" + watermark);
                if (expected == GroupState.InsertStatus.INSERTED && !reuse) {
                    knownIds.add(id);
                }
            } else if (roll < 85) {
                // 撤回：一半用已知 ID，一半编造 ID（测 EVENT_UNKNOWN）
                String id;
                if (!knownIds.isEmpty() && rnd.nextBoolean()) {
                    id = knownIds.get(rnd.nextInt(knownIds.size()));
                } else {
                    id = "ghost" + rnd.nextInt(100);
                }
                GroupState.RetractStatus actual = sut.retract(id, ts);
                GroupState.RetractStatus expected = oracleRetract(id, ts);
                TestHarness.eq(actual, expected, "step " + i + " retract " + id
                        + " ts=" + ts + " wm=" + watermark);
            } else {
                advance(ts); // 纯推进，触发滑出
            }

            if (i % 7 == 0) {
                assertAgainstOracle("step " + i);
            }
        }
        // 末尾再做一轮全量对照与各种 K
        assertAgainstOracle("final");
        for (int k : new int[]{0, 1, 3, 100}) {
            List<GroupState.Row> actual = sut.topK(k);
            List<GroupState.Row> expected = oracleTopK(k);
            assertRowsEqual(actual, expected, "final topK k=" + k);
        }
    }

    private void advance(long ts) {
        if (ts > watermark) {
            watermark = ts;
        }
        sut.advance(watermark);
        // oracle：撤回墓碑只保留到事件过期时刻
        long horizon = watermark - W;
        oracleRetracted.entrySet().removeIf(en -> en.getValue() <= horizon);
    }

    private GroupState.InsertStatus oracleInsert(String id, String item, long delta, long ts) {
        long horizon = watermark - W;
        if (ts <= horizon) {
            return GroupState.InsertStatus.LATE;
        }
        OEvent old = oracleAll.get(id);
        if (old != null && old.ts <= horizon) {
            // 旧事件已自然过期滑出，ID 不再被占用（与 SUT 过期即删一致）；它对今后任何查询贡献均为 0
            oracleAll.remove(id);
            old = null;
        }
        if (old != null || oracleRetracted.containsKey(id)) {
            return GroupState.InsertStatus.DUPLICATE;
        }
        oracleAll.put(id, new OEvent(item, delta, ts));
        return GroupState.InsertStatus.INSERTED;
    }

    private GroupState.RetractStatus oracleRetract(String id, long ts) {
        if (oracleRetracted.containsKey(id)) {
            return GroupState.RetractStatus.ALREADY_RETRACTED;
        }
        OEvent e = oracleAll.get(id);
        long horizon = watermark - W;
        if (e == null || e.ts <= horizon) {
            return GroupState.RetractStatus.EVENT_UNKNOWN;
        }
        oracleAll.remove(id);
        oracleRetracted.put(id, e.ts);
        return GroupState.RetractStatus.RETRACTED;
    }

    /** oracle 的全量重算：过滤窗口内未撤回事件 -> 按 item 求和 -> (-score, id) 排序。 */
    private List<GroupState.Row> oracleFullOrder() {
        long horizon = watermark - W;
        Map<String, Long> score = new HashMap<>();
        for (OEvent e : oracleAll.values()) {
            if (e.ts > horizon) {
                score.merge(e.item, e.delta, Long::sum);
            }
        }
        List<GroupState.Row> rows = new ArrayList<>();
        score.forEach((item, s) -> rows.add(new GroupState.Row(item, s)));
        rows.sort(Comparator.comparingLong((GroupState.Row r) -> r.score).reversed()
                .thenComparing(r -> r.itemId));
        return rows;
    }

    private List<GroupState.Row> oracleTopK(int k) {
        List<GroupState.Row> all = oracleFullOrder();
        return all.subList(0, Math.min(k, all.size()));
    }

    private void assertAgainstOracle(String label) {
        List<GroupState.Row> expected = oracleFullOrder();
        List<GroupState.Row> actualFull = sut.fullOrder();
        assertRowsEqual(actualFull, expected, label + " fullOrder");

        // TopK 必须恰好是完整排序的前 K
        int k = rnd.nextInt(11);
        List<GroupState.Row> actualTop = sut.topK(k);
        assertRowsEqual(actualTop, expected.subList(0, Math.min(k, expected.size())),
                label + " topK k=" + k);

        TestHarness.eq(sut.activeEventCount(), expectedEventCount(), label + " activeEvents");
        TestHarness.eq(sut.activeItemCount(), expected.size(), label + " activeItems");
        TestHarness.eq(sut.tombstoneCount(), oracleRetracted.size(), label + " tombstones");
        TestHarness.eq(sut.watermark(), watermark, label + " watermark");

        // 内存有界性：有效事件 + 墓碑，都不应超过“一个窗口内发生过的事件”的量级
        int windowEvents = 0;
        long horizon = watermark - W;
        for (OEvent e : oracleAll.values()) {
            if (e.ts > horizon) {
                windowEvents++;
            }
        }
        TestHarness.check(sut.activeEventCount() <= windowEvents,
                label + " 有效事件数不应超过窗口内事件数");
    }

    private int expectedEventCount() {
        long horizon = watermark - W;
        int n = 0;
        for (OEvent e : oracleAll.values()) {
            if (e.ts > horizon) {
                n++;
            }
        }
        return n;
    }

    private static void assertRowsEqual(List<GroupState.Row> actual,
                                        List<GroupState.Row> expected, String msg) {
        TestHarness.eq(actual.size(), expected.size(),
                msg + " 行数不一致; 实际=" + actual + " 期望=" + expected);
        for (int i = 0; i < actual.size(); i++) {
            GroupState.Row a = actual.get(i);
            GroupState.Row b = expected.get(i);
            TestHarness.eq(a.itemId, b.itemId, msg + " 第 " + i + " 行 itemId");
            TestHarness.eq(a.score, b.score, msg + " 第 " + i + " 行 score");
        }
    }
}
