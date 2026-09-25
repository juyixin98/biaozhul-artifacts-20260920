package streamagg.tests;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;

import streamagg.core.ApplyResult;
import streamagg.core.EventOp;
import streamagg.core.KeyStats;
import streamagg.core.OpType;
import streamagg.core.StreamProcessor;

/**
 * 属性测试：随机生成大量事件操作流（含显式版本乱序、重复消息、撤销先行），
 * 每一步都用“独立参考实现”对最终账本做精确重算并与增量状态比较。
 */
public final class PropertyTest {

    /** 参考侧单个事件状态（独立于引擎代码）。 */
    private static final class RefEvent {
        boolean active;
        String key;
        BigDecimal value;
        long lastVersion;
        final Map<Long, EventOp> all = new HashMap<>();
    }

    public static void main(String[] args) {
        TestFramework t = new TestFramework();
        t.test("属性：200 个随机序列种子，逐步账本重算均一致", () -> runProperty(t, 200));
        t.test("属性：高密度重复消息（opId 重发）不改变结果", () -> runDuplicateHeavy(t));
        int code = t.summary("PropertyTest");
        if (code != 0) {
            System.exit(code);
        }
    }

    private static void runProperty(TestFramework t, int seeds) {
        for (int seed = 1; seed <= seeds; seed++) {
            Random rnd = new Random(seed * 7919L + 13);
            StreamProcessor p = new StreamProcessor();
            Map<String, RefEvent> ref = new HashMap<>();
            int eventCount = 2 + rnd.nextInt(6);
            List<String> eventIds = new ArrayList<>();
            for (int i = 0; i < eventCount; i++) {
                eventIds.add("e" + i);
            }
            String[] keys = {"a", "b", "c"};
            // 每个事件已提交/计划的最大版本
            Map<String, Long> maxPlanned = new HashMap<>();
            // opId 去重表（模拟客户端重试）
            Map<EventKey, EventOp> sentOnce = new HashMap<>();

            int steps = 60 + rnd.nextInt(120);
            for (int step = 0; step < steps; step++) {
                String eid = eventIds.get(rnd.nextInt(eventIds.size()));
                RefEvent re = ref.computeIfAbsent(eid, k -> new RefEvent());
                long usedVersion = maxPlanned.getOrDefault(eid, 0L);

                // 10% 概率重发一条历史消息（客户端重试）-> 必须被引擎幂等处理
                EventOp op;
                if (!sentOnce.isEmpty() && rnd.nextInt(10) == 0) {
                    List<EventKey> keyList = new ArrayList<>(sentOnce.keySet());
                    EventKey pick = keyList.get(rnd.nextInt(keyList.size()));
                    if (!pick.eventId.equals(eid)) {
                        eid = pick.eventId;
                        re = ref.computeIfAbsent(eid, k -> new RefEvent());
                    }
                    op = sentOnce.get(pick);
                } else {
                    // 为该事件挑一个新版本：允许“跳号”（后续操作制造乱序补洞）
                    long v = usedVersion + 1;
                    if (rnd.nextInt(4) == 0 && usedVersion >= 1) {
                        // 跳 1~2 个版本，稍后会以更旧版本补洞
                        v += 1 + rnd.nextInt(2);
                    }
                    OpType type = nextType(rnd, usedVersion == 0);
                    String key = keys[rnd.nextInt(keys.length)];
                    BigDecimal value = scaled(rnd, -50, 150);
                    op = switch (type) {
                        case ADD -> EventOp.add(eid, key, value, v, null);
                        case RETRACT -> EventOp.retract(eid, v, null);
                        case CORRECT -> EventOp.correct(eid, key, scaled(rnd, -50, 150), v, null);
                    };
                    maxPlanned.put(eid, Math.max(usedVersion, v));
                    sentOnce.put(new EventKey(eid, v), op);
                }

                ApplyResult result;
                try {
                    result = p.submit(op);
                } catch (RuntimeException ex) {
                    throw new AssertionError("seed=" + seed + " step=" + step
                            + " op=" + op + " 引擎抛异常: " + ex);
                }
                applyToReference(re, op, result);

                // 每一步都做独立账本重算比较
                Map<String, KeyStats> expected = referenceRecompute(ref);
                Map<String, KeyStats> actual = p.allStats();
                if (!mapsEqual(expected, actual)) {
                    throw new AssertionError("seed=" + seed + " step=" + step
                            + "\n  期望 " + expected + "\n  实际 " + actual
                            + "\n  op=" + op + " result=" + result.status());
                }
            }

            // 收尾：把每个事件跳号造成的空洞全部补齐，保证最终无挂起
            for (String eid : eventIds) {
                fillGaps(p, ref, eid, sentOnce);
            }
            for (String eid : eventIds) {
                if (p.pendingOf(eid).size() != 0) {
                    throw new AssertionError("seed=" + seed + " 事件 " + eid + " 最终仍有挂起");
                }
            }
            Map<String, KeyStats> expected = referenceRecompute(ref);
            if (!mapsEqual(expected, p.allStats())) {
                throw new AssertionError("seed=" + seed + " 最终不一致: "
                        + expected + " vs " + p.allStats());
            }
            var rr = p.reconcile();
            if (!rr.consistent()) {
                throw new AssertionError("seed=" + seed + " 对账失败: " + rr.detail());
            }
            var replay = p.replay();
            if (!replay.consistent()) {
                throw new AssertionError("seed=" + seed + " 重放失败: " + replay.detail());
            }
        }
    }

    /** 参考侧仅在操作实际“按序应用”（非冲突拒绝）时更新；冲突/重复与引擎语义保持一致。 */
    private static void applyToReference(RefEvent re, EventOp op, ApplyResult result) {
        if (result.status() == ApplyResult.Status.STALE_CONFLICT) {
            return;
        }
        // 必须用操作自身的版本号（属性测试全部带显式版本）；result.version() 在级联
        // 排空缓存时是“最后应用版本”，不能当作本次操作的版本。
        long v = op.version();
        if (re.all.containsKey(v)) {
            // 重复版本：参考侧也幂等
            return;
        }
        re.all.put(v, op);
        // 参考侧按版本顺序重放整个连续前缀（空洞版本不产生状态变更，与引擎 BUFFERED 对齐）
        replayPrefix(re);
    }

    private static void replayPrefix(RefEvent re) {
        re.active = false;
        re.key = null;
        re.value = null;
        long v = 1;
        while (re.all.containsKey(v)) {
            EventOp op = re.all.get(v);
            switch (op.type()) {
                case ADD -> {
                    re.active = true;
                    re.key = op.key();
                    re.value = op.value();
                }
                case RETRACT -> {
                    re.active = false;
                    re.key = null;
                    re.value = null;
                }
                case CORRECT -> {
                    String nk = op.key() != null ? op.key() : re.key;
                    if (!re.active) {
                        re.active = true;
                    }
                    re.key = nk;
                    re.value = op.value();
                }
            }
            re.lastVersion = v;
            v++;
        }
    }

    private static void fillGaps(StreamProcessor p, Map<String, RefEvent> ref,
                                 String eid, Map<EventKey, EventOp> sentOnce) {
        RefEvent re = ref.get(eid);
        if (re == null) {
            return;
        }
        // 用“无版本操作”补齐剩余空洞最简单：引擎会自动找最小空位。
        // 为保持参考侧同步，直接按缺失版本顺序提交占位操作（CORRECT/ADD 到任意键）
        // 找到 sentOnce 中该事件的最大版本
        long max = 0;
        for (EventKey k : sentOnce.keySet()) {
            if (k.eventId.equals(eid)) {
                max = Math.max(max, k.version);
            }
        }
        for (long v = 1; v <= max; v++) {
            if (!re.all.containsKey(v)) {
                // 空洞版本：发一个 CORRECT 补上（值固定，参考侧同样记录）
                EventOp fill = EventOp.correct(eid, "a", BigDecimal.ONE, v, null);
                ApplyResult r = p.submit(fill);
                if (r.status() == ApplyResult.Status.STALE_CONFLICT) {
                    continue;
                }
                sentOnce.put(new EventKey(eid, v), fill);
                applyToReference(re, fill, r);
            }
        }
    }

    private static void runDuplicateHeavy(TestFramework t) {
        StreamProcessor p = new StreamProcessor();
        for (int i = 0; i < 50; i++) {
            EventOp op = EventOp.add("dup", "a", bd("1"), 1L, "fixed-op-id");
            var r = p.submit(op);
            if (i > 0 && r.status() != ApplyResult.Status.DUPLICATE) {
                throw new AssertionError("第 " + i + " 次重发应为 DUPLICATE，实际 " + r.status());
            }
        }
        t.eqKeyStats(p.statsOf("a"), 1, "1", "重发 50 次只计一次");
        var rr = p.reconcile();
        t.eq(rr.consistent(), true, "重发场景一致");
        t.eq(p.journal().size(), 1, "日志只记 1 条");
    }

    private static Map<String, KeyStats> referenceRecompute(Map<String, RefEvent> ref) {
        // 只统计连续前缀完整（即引擎视角已应用）的存活事件；
        // RefEvent 状态在 replayPrefix 中已按空洞语义更新
        Map<String, long[]> counts = new HashMap<>();
        Map<String, BigDecimal> sums = new HashMap<>();
        for (RefEvent re : ref.values()) {
            if (!re.active || re.key == null) {
                continue;
            }
            counts.computeIfAbsent(re.key, k -> new long[]{0})[0]++;
            sums.merge(re.key, re.value, BigDecimal::add);
        }
        Map<String, KeyStats> out = new HashMap<>();
        for (Map.Entry<String, BigDecimal> e : sums.entrySet()) {
            long c = counts.get(e.getKey())[0];
            if (c < 0) {
                throw new IllegalStateException("参考实现检测到负漂移");
            }
            out.put(e.getKey(), new KeyStats(c, e.getValue()));
        }
        return out;
    }

    private static boolean mapsEqual(Map<String, KeyStats> a, Map<String, KeyStats> b) {
        if (!a.keySet().equals(b.keySet())) {
            return false;
        }
        for (String k : a.keySet()) {
            KeyStats x = a.get(k);
            KeyStats y = b.get(k);
            if (x.count() != y.count() || x.sum().compareTo(y.sum()) != 0) {
                return false;
            }
        }
        return true;
    }

    private static OpType nextType(Random rnd, boolean first) {
        // 生命周期开始时给较高 ADD 概率
        int roll = rnd.nextInt(10);
        if (first) {
            return OpType.ADD;
        }
        if (roll < 5) {
            return OpType.CORRECT;
        }
        if (roll < 8) {
            return OpType.ADD;
        }
        return OpType.RETRACT;
    }

    private static BigDecimal scaled(Random rnd, int min, int max) {
        long unscaled = min * 100L + (long) (rnd.nextDouble() * ((max - min) * 100));
        return BigDecimal.valueOf(unscaled).movePointLeft(2);
    }

    private static BigDecimal bd(String s) {
        return new BigDecimal(s);
    }

    private record EventKey(String eventId, long version) {
    }
}
