package intervaljoin;

import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.NavigableMap;
import java.util.TreeMap;

/**
 * 双流区间连接引擎（事件时间语义，单线程访问）。
 *
 * 连接条件（区间两端均为闭区间）：
 *   同 key 且  lowerBound &lt;= r.ts - l.ts &lt;= upperBound
 *
 * 水位语义：watermark = w 断言 “事件时间 &lt; w 的事件都已到达”，
 * 因此严格小于水位的迟到事件被丢弃，ts == watermark 的事件仍算准时。
 *
 * 回收条件（必须“双方水位”同时证明无用，单侧推进不回收）：
 *   有效水位 wm = min(leftWatermark, rightWatermark)
 *   左事件 l 可回收  &lt;=&gt;  未来右事件 r 不可能再命中，即 r.ts &gt; l.ts + upperBound；
 *                        在右流水位语义下 r.ts &gt;= wm（wm 仍可能到达），
 *                        故保留到 l.ts + upperBound &lt; wm，即 l.ts &lt; wm - upperBound。
 *   右事件 r 可回收  &lt;=&gt;  未来左事件 l 不可能再命中，即 l.ts &gt; r.ts - lowerBound；
 *                        故保留到 r.ts - lowerBound &lt; wm，即 r.ts &lt; wm + lowerBound。
 *
 * 每对恰好输出一次：先到的事件进状态，后到的事件只探测对方状态。
 * 即使状态尚未回收（对侧水位落后），新事件也不会在自己的状态里
 * 与“已在状态中的另一侧事件”重复配对——配对只朝一个方向发生。
 */
public final class JoinEngine {

    /** 每个 key 一份状态：事件时间 -&gt; 该时刻的事件（同刻多事件用 list 保存）。 */
    private static final class KeyState {
        final TreeMap<Long, List<Event>> byTime = new TreeMap<>();
        long count;
    }

    private final TreeMap<String, KeyState> leftState = new TreeMap<>();
    private final TreeMap<String, KeyState> rightState = new TreeMap<>();

    private long lowerBound;
    private long upperBound;

    private long leftWm = Long.MIN_VALUE;
    private long rightWm = Long.MIN_VALUE;

    private final Stats stats = new Stats();

    public JoinEngine(long lowerBound, long upperBound) {
        if (lowerBound > upperBound) {
            throw new IllegalArgumentException("lowerBound > upperBound");
        }
        this.lowerBound = lowerBound;
        this.upperBound = upperBound;
    }

    public long getLowerBound() { return lowerBound; }
    public long getUpperBound() { return upperBound; }
    public long getLeftWatermark() { return leftWm; }
    public long getRightWatermark() { return rightWm; }
    public Stats getStats() { return stats; }

    /** 当前两侧仍驻留的事件总数。 */
    public long retainedEvents() {
        long n = 0;
        for (KeyState ks : leftState.values()) n += ks.count;
        for (KeyState ks : rightState.values()) n += ks.count;
        return n;
    }

    public int retainedKeys() { return leftState.size() + rightState.size(); }

    /**
     * 配置连接区间。只允许在没有任何状态/水位时（启动或 reset 后）修改。
     */
    public void configure(long lower, long upper) {
        if (lower > upper) {
            throw new IllegalArgumentException("lowerBound > upperBound");
        }
        if (retainedEvents() != 0 || leftWm != Long.MIN_VALUE || rightWm != Long.MIN_VALUE) {
            throw new IllegalStateException(
                    "cannot reconfigure after state exists; reset() first");
        }
        this.lowerBound = lower;
        this.upperBound = upper;
    }

    /** 接收一条事件。迟到（ts &lt; 本侧水位）的事件直接丢弃并计数。返回本次新产生的配对。 */
    public List<Pair> ingest(Event e) {
        List<Pair> out = new ArrayList<>();
        if ("left".equals(e.side)) {
            if (e.ts < leftWm) {
                stats.leftLateDropped++;
                return out;
            }
            stats.leftAccepted++;
            // 新左事件探测右状态：r.ts ∈ [l.ts+lower, l.ts+upper]
            long lo = satAdd(e.ts, lowerBound);
            long hi = satAdd(e.ts, upperBound);
            probe(rightState, e.key, lo, hi, e, true, out);
            put(leftState, e);
        } else {
            if (e.ts < rightWm) {
                stats.rightLateDropped++;
                return out;
            }
            stats.rightAccepted++;
            // 新右事件探测左状态：l.ts ∈ [r.ts-upper, r.ts-lower]
            long lo = satSub(e.ts, upperBound);
            long hi = satSub(e.ts, lowerBound);
            probe(leftState, e.key, lo, hi, e, false, out);
            put(rightState, e);
        }
        stats.pairsEmitted += out.size();
        return out;
    }

    /** 推进左流水位（不允许回退），随后按有效水位回收双方状态。返回回收事件总数。 */
    public long advanceLeftWatermark(long wm) {
        if (wm < leftWm) {
            throw new IllegalArgumentException("watermark cannot go backwards: "
                    + leftWm + " -> " + wm);
        }
        leftWm = wm;
        return purge();
    }

    /** 推进右流水位，语义同 {@link #advanceLeftWatermark}。 */
    public long advanceRightWatermark(long wm) {
        if (wm < rightWm) {
            throw new IllegalArgumentException("watermark cannot go backwards: "
                    + rightWm + " -> " + wm);
        }
        rightWm = wm;
        return purge();
    }

    /** 清空全部状态、水位与统计（保留区间配置）。 */
    public void reset() {
        leftState.clear();
        rightState.clear();
        leftWm = Long.MIN_VALUE;
        rightWm = Long.MIN_VALUE;
        Stats s = stats;
        s.leftAccepted = s.rightAccepted = 0;
        s.leftLateDropped = s.rightLateDropped = 0;
        s.pairsEmitted = 0;
        s.leftPurged = s.rightPurged = 0;
        s.leftKeySlotsRemoved = s.rightKeySlotsRemoved = 0;
    }

    // ------------------------------------------------------------------
    // 内部实现
    // ------------------------------------------------------------------

    /**
     * 用新事件探测对侧状态里时间落在 [lo, hi] 的事件。
     * @param newIsLeft 新事件是否为左事件；为 true 时新事件在左，被探测的在右。
     */
    private void probe(TreeMap<String, KeyState> other, String key,
                       long lo, long hi, Event incoming, boolean newIsLeft,
                       List<Pair> out) {
        KeyState ks = other.get(key);
        if (ks == null) return;
        NavigableMap<Long, List<Event>> range = ks.byTime.subMap(lo, true, hi, true);
        if (newIsLeft) {
            for (List<Event> bucket : range.values()) {
                for (Event r : bucket) out.add(new Pair(incoming, r));
            }
        } else {
            for (List<Event> bucket : range.values()) {
                for (Event l : bucket) out.add(new Pair(l, incoming));
            }
        }
    }

    private static void put(TreeMap<String, KeyState> state, Event e) {
        KeyState ks = state.computeIfAbsent(e.key, k -> new KeyState());
        ks.byTime.computeIfAbsent(e.ts, t -> new ArrayList<>()).add(e);
        ks.count++;
    }

    /**
     * 按有效水位 min(leftWm, rightWm) 回收。
     * 单侧水位推进时，若另一侧落后，有效水位不动，回收量为 0 ——
     * 这正是“双方水位共同证明不再需要”的体现。
     */
    private long purge() {
        long wm = Math.min(leftWm, rightWm);
        if (wm == Long.MIN_VALUE) return 0;

        long before = stats.leftPurged + stats.rightPurged;

        // 左事件：l.ts < wm - upperBound。
        // upperBound >= 0，wm - upperBound 不可能溢出（只会向更小方向移动）。
        long leftCutoffExclusive = wm - upperBound;
        purgeSide(leftState, leftCutoffExclusive, true);

        // 右事件：r.ts < wm + lowerBound
        long rightCutoffExclusive = satAdd(wm, lowerBound);
        purgeSide(rightState, rightCutoffExclusive, false);

        return (stats.leftPurged + stats.rightPurged) - before;
    }

    private void purgeSide(TreeMap<String, KeyState> state, long cutoffExclusive, boolean isLeft) {
        // cutoffExclusive 表示“严格小于此时间戳”的事件可删。
        // cutoffExclusive == Long.MIN_VALUE 时没有任何事件严格小于它，直接跳过。
        if (cutoffExclusive == Long.MIN_VALUE) return;
        Iterator<Map.Entry<String, KeyState>> it = state.entrySet().iterator();
        while (it.hasNext()) {
            Map.Entry<String, KeyState> entry = it.next();
            KeyState ks = entry.getValue();
            NavigableMap<Long, List<Event>> dead =
                    ks.byTime.headMap(cutoffExclusive, false);
            if (dead.isEmpty()) continue;
            long removed = 0;
            for (List<Event> bucket : dead.values()) removed += bucket.size();
            dead.clear();
            ks.count -= removed;
            if (isLeft) {
                stats.leftPurged += removed;
            } else {
                stats.rightPurged += removed;
            }
            if (ks.byTime.isEmpty()) {
                it.remove();
                if (isLeft) stats.leftKeySlotsRemoved++;
                else stats.rightKeySlotsRemoved++;
            }
        }
    }

    private static long satAdd(long a, long b) {
        long r = a + b;
        if (((a ^ r) & (b ^ r)) < 0) {
            return a > 0 ? Long.MAX_VALUE : Long.MIN_VALUE;
        }
        return r;
    }

    private static long satSub(long a, long b) {
        long r = a - b;
        if (((a ^ b) & (a ^ r)) < 0) {
            return a > 0 ? Long.MAX_VALUE : Long.MIN_VALUE;
        }
        return r;
    }
}
