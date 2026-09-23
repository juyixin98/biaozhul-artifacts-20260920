package ij;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.NavigableMap;
import java.util.Set;
import java.util.TreeMap;

/**
 * 双流水位区间连接引擎（单个会话）。
 *
 * 配对条件：同 key 且 l.ts + lowerBound &lt;= r.ts &lt;= l.ts + upperBound（闭区间，含边界）。
 * 迟到事件（eventTime &lt;= 本侧当前水位）直接丢弃，不参与连接、不进状态。
 *
 * 状态回收必须同时被“双方水位”证明不再需要：
 *   - 左事件 l 可回收当且仅当：wl &gt;= l.ts（本侧不会再来更晚的、能与 l 配对的右事件需要靠 wl 证明迟到）
 *     且 wr &gt;= l.ts + upperBound（右侧任何未来通过水位的事件 r 都有 r.ts &gt; wr &gt;= 上界，不可能命中 l）。
 *     截断阈值：cutL = min(wl, wr - upperBound)。
 *   - 右事件 r 可回收当且仅当：wr &gt;= r.ts 且 wl &gt;= r.ts - lowerBound（w 不回退 ⇒ 未来左事件 l 满足
 *     l.ts &gt; wl ⇒ r.ts &lt; l.ts + lowerBound，不可能命中 r）。截断阈值：cutR = min(wr, wl - lowerBound)。
 *   - 水位初始为 Long.MIN_VALUE 时阈值不超过任何事件时间，不会误回收。
 *
 * 每一侧水位推进都同时尝试回收两侧状态（任一侧水位前进都可能解锁对侧清理）。
 */
final class JoinSession {

    private static final long UNSET_WM = Long.MIN_VALUE;

    final String id;
    private final long lowerBound;
    private final long upperBound;

    /** key -> (事件时间 -> 该时间点的事件列表)，同时间可有多条事件。 */
    private final Map<String, TreeMap<Long, List<Event>>> leftState = new LinkedHashMap<>();
    private final Map<String, TreeMap<Long, List<Event>>> rightState = new LinkedHashMap<>();

    /** 已输出配对去重：leftId + '|' + rightId。 */
    private final Set<String> emitted = new HashSet<>();
    private final List<Pair> pairs = new ArrayList<>();

    private long wmLeft = UNSET_WM;
    private long wmRight = UNSET_WM;

    private long seq = 0;
    private long leftReceived;
    private long rightReceived;
    private long leftDropped;
    private long rightDropped;
    private long leftReclaimed;
    private long rightReclaimed;

    JoinSession(String id, long lowerBound, long upperBound) {
        if (lowerBound > upperBound) {
            throw new IllegalArgumentException("lowerBound must be <= upperBound");
        }
        this.id = id;
        this.lowerBound = lowerBound;
        this.upperBound = upperBound;
    }

    String id() {
        return id;
    }

    synchronized EventResult pushEvent(String side, String key, Long ts, String clientId, Object payload) {
        if (key == null) {
            throw new IllegalArgumentException("key is required");
        }
        if (ts == null) {
            throw new IllegalArgumentException("ts is required");
        }
        if (!"left".equals(side) && !"right".equals(side)) {
            throw new IllegalArgumentException("side must be left or right");
        }
        boolean isLeft = "left".equals(side);
        long wm = isLeft ? wmLeft : wmRight;
        if (isLeft) {
            leftReceived++;
        } else {
            rightReceived++;
        }

        EventResult result;
        // 水位语义：watermark t 断言“eventTime <= t 的事件已全部到达”，故 eventTime <= wm 为迟到。
        if (ts <= wm) {
            if (isLeft) {
                leftDropped++;
            } else {
                rightDropped++;
            }
            return new EventResult(clientId, true);
        }

        String eventId = clientId != null ? clientId : (isLeft ? "L#" : "R#") + (++seq);
        Event event = new Event(eventId, key, ts, payload);
        result = new EventResult(eventId, false);

        if (isLeft) {
            // 与已缓存右事件做区间连接。
            TreeMap<Long, List<Event>> bucket = rightState.get(key);
            if (bucket != null) {
                long lo = safeAdd(ts, lowerBound);
                long hi = safeAdd(ts, upperBound);
                for (List<Event> group : safeSubMap(bucket, lo, hi)) {
                    for (Event r : group) {
                        emit(event, r, result.newPairs);
                    }
                }
            }
            insert(leftState, key, event);
        } else {
            TreeMap<Long, List<Event>> bucket = leftState.get(key);
            if (bucket != null) {
                // r.ts ∈ [l.ts+lower, l.ts+upper] ⇔ l.ts ∈ [r.ts-upper, r.ts-lower]
                long lo = safeAdd(ts, -upperBound);
                long hi = safeAdd(ts, -lowerBound);
                for (List<Event> group : safeSubMap(bucket, lo, hi)) {
                    for (Event l : group) {
                        emit(l, event, result.newPairs);
                    }
                }
            }
            insert(rightState, key, event);
        }
        return result;
    }

    /** 推进水位；不回退（小于等于当前水位的值直接忽略）。返回本次回收事件数。 */
    synchronized int advanceWatermark(String side, long newWm) {
        if ("left".equals(side)) {
            if (newWm <= wmLeft) {
                return 0;
            }
            wmLeft = newWm;
        } else if ("right".equals(side)) {
            if (newWm <= wmRight) {
                return 0;
            }
            wmRight = newWm;
        } else {
            throw new IllegalArgumentException("side must be left or right");
        }
        return evictLeft() + evictRight();
    }

    synchronized List<Pair> pairs() {
        return new ArrayList<>(pairs);
    }

    synchronized Map<String, Object> snapshotStats() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("sessionId", id);
        m.put("lowerBound", lowerBound);
        m.put("upperBound", upperBound);
        m.put("watermarkLeft", wmLeft == UNSET_WM ? null : wmLeft);
        m.put("watermarkRight", wmRight == UNSET_WM ? null : wmRight);
        m.put("leftReceived", leftReceived);
        m.put("rightReceived", rightReceived);
        m.put("leftDroppedLate", leftDropped);
        m.put("rightDroppedLate", rightDropped);
        m.put("leftStateEvents", countState(leftState));
        m.put("rightStateEvents", countState(rightState));
        m.put("leftStateKeys", leftState.size());
        m.put("rightStateKeys", rightState.size());
        m.put("leftReclaimed", leftReclaimed);
        m.put("rightReclaimed", rightReclaimed);
        m.put("totalReclaimed", leftReclaimed + rightReclaimed);
        m.put("pairsEmitted", pairs.size());
        return m;
    }

    // ---------------- 内部实现 ----------------

    private void emit(Event l, Event r, List<Pair> sink) {
        String dedup = l.id + "|" + r.id;
        if (emitted.add(dedup)) {
            Pair p = new Pair(l, r);
            pairs.add(p);
            sink.add(p);
        }
    }

    private static void insert(Map<String, TreeMap<Long, List<Event>>> state, String key, Event e) {
        state.computeIfAbsent(key, k -> new TreeMap<>())
                .computeIfAbsent(e.ts, t -> new ArrayList<>())
                .add(e);
    }

    private int evictLeft() {
        // cutL = min(wl, wr - upperBound)，移除 ts <= cutL 的左事件。
        long opp = safeAdd(wmRight, -upperBound);
        long cut = Math.min(wmLeft, opp);
        if (cut == UNSET_WM) {
            return 0;
        }
        int reclaimed = 0;
        var it = leftState.entrySet().iterator();
        while (it.hasNext()) {
            Map.Entry<String, TreeMap<Long, List<Event>>> e = it.next();
            TreeMap<Long, List<Event>> bucket = e.getValue();
            NavigableMap<Long, List<Event>> expired = bucket.headMap(cut, true);
            int n = 0;
            for (List<Event> group : expired.values()) {
                n += group.size();
            }
            if (!expired.isEmpty()) {
                expired.clear();
            }
            reclaimed = Math.addExact(reclaimed, n);
            leftReclaimed += n;
            if (bucket.isEmpty()) {
                it.remove();
            }
        }
        return reclaimed;
    }

    private int evictRight() {
        // cutR = min(wr, wl - lowerBound)，移除 ts <= cutR 的右事件。
        long opp = safeAdd(wmLeft, -lowerBound);
        long cut = Math.min(wmRight, opp);
        if (cut == UNSET_WM) {
            return 0;
        }
        int reclaimed = 0;
        var it = rightState.entrySet().iterator();
        while (it.hasNext()) {
            Map.Entry<String, TreeMap<Long, List<Event>>> e = it.next();
            TreeMap<Long, List<Event>> bucket = e.getValue();
            NavigableMap<Long, List<Event>> expired = bucket.headMap(cut, true);
            int n = 0;
            for (List<Event> group : expired.values()) {
                n += group.size();
            }
            if (!expired.isEmpty()) {
                expired.clear();
            }
            reclaimed = Math.addExact(reclaimed, n);
            rightReclaimed += n;
            if (bucket.isEmpty()) {
                it.remove();
            }
        }
        return reclaimed;
    }

    private static Iterable<List<Event>> safeSubMap(TreeMap<Long, List<Event>> bucket, long lo, long hi) {
        if (lo > hi) {
            return List.of();
        }
        return bucket.subMap(lo, true, hi, true).values();
    }

    private static long safeAdd(long a, long b) {
        long r = a + b;
        // 溢出钳制。
        if (b > 0 && r < a) {
            return Long.MAX_VALUE;
        }
        if (b < 0 && r > a) {
            return Long.MIN_VALUE;
        }
        return r;
    }

    private static long countState(Map<String, ? extends Map<Long, List<Event>>> state) {
        long n = 0;
        for (Map<Long, List<Event>> bucket : state.values()) {
            for (List<Event> group : bucket.values()) {
                n += group.size();
            }
        }
        return n;
    }
}
