package com.tjoin.core;

import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.NavigableMap;
import java.util.Set;
import java.util.TreeMap;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 按键（keyed）双流区间连接算子。
 *
 * <p>连接条件（可配闭/开边界，默认闭区间）：
 * <pre>
 *     L.ts + lowerBound &lt;= R.ts &lt;= L.ts + upperBound
 * </pre>
 *
 * <h2>状态与水位线</h2>
 * <ul>
 *   <li>每个 key 独立维护左右两侧缓冲，按事件时间戳组织（{@link TreeMap}），
 *       因此缓冲量只与“仍可能匹配的时间跨度”有关。</li>
 *   <li>两侧水位线各自独立推进。清理由<b>对侧</b>水位线驱动：
 *     <ul>
 *       <li>左事件 L 仅当右水位线已超过它可能匹配的最大右时间戳时才删除
 *           （{@code wmR - L.ts} 越过 upperBound）；</li>
 *       <li>右事件 R 仅当左水位线使任何未来左事件都无法满足下界时才删除
 *           （{@code R.ts - wmL} 低于 lowerBound）。</li>
 *     </ul>
 *     这保证了“不会提前丢弃仍可匹配的记录”：一侧停滞时，另一侧水位线不动，
 *     其对侧状态一律保留。</li>
 *   <li>初始水位线为 {@code Long.MIN_VALUE}，即默认所有事件都不算迟到。</li>
 *   <li>迟到事件（{@code ts < 本侧水位线}）被丢弃并计数；不产生输出。</li>
 * </ul>
 *
 * <h2>去重</h2>
 * 允许值重复，但以事件唯一 ID 去重：重复投递（同一侧、同一 ID）只处理一次。
 * 已见 ID 在其事件时间戳低于本侧水位线后才随清理淘汰，
 * 因此过期事件的重放也不会导致重复输出。天然地，每对事件最多输出一次。
 *
 * <p>本类非线程安全（事件/水位线应串行驱动，与单分区处理模型一致）。
 */
public final class IntervalJoinOperator {

    /** 初始水位线：任何有限时间戳的事件都不算迟到。 */
    public static final long INITIAL_WATERMARK = Long.MIN_VALUE;

    private final JoinConfig config;
    private final JoinMetrics metrics;
    private final Map<String, KeyedState> states = new ConcurrentHashMap<>();

    private long leftWatermark = INITIAL_WATERMARK;
    private long rightWatermark = INITIAL_WATERMARK;

    public IntervalJoinOperator(JoinConfig config) {
        this(config, new JoinMetrics());
    }

    public IntervalJoinOperator(JoinConfig config, JoinMetrics metrics) {
        this.config = config;
        this.metrics = metrics;
    }

    public JoinConfig config() {
        return config;
    }

    public JoinMetrics metrics() {
        return metrics;
    }

    public long watermark(StreamSide side) {
        return side == StreamSide.LEFT ? leftWatermark : rightWatermark;
    }

    /** 下游观察到的水位线 = 两侧水位线的较小值。 */
    public long outputWatermark() {
        return Math.min(leftWatermark, rightWatermark);
    }

    /** 当前某一侧跨所有 key 缓冲的事件总数。 */
    public int bufferedCount(StreamSide side) {
        int total = 0;
        for (KeyedState st : states.values()) {
            total += st.bufferedCount(side);
        }
        return total;
    }

    /** 活跃 key 数（测试/诊断用）。 */
    public int keyCount() {
        return states.size();
    }

    /**
     * 处理一个事件。
     *
     * @return 本次事件直接产生的连接结果（可能为空列表，永不为 null）；
     *         重复或迟到事件返回空列表
     * @throws BufferCapacityExceededException 若本侧缓冲已达配置上限
     */
    public List<JoinPair> processEvent(Event event, Collector collector) {
        StreamSide side = event.side();
        metrics.incrementReceived(side);

        KeyedState st = states.computeIfAbsent(event.key(), k -> new KeyedState());

        // 1) 重复事件（按唯一 ID，同侧重放）——只处理一次
        if (st.isSeen(side, event.id())) {
            metrics.incrementDuplicates();
            return List.of();
        }

        // 2) 迟到事件（事件时间小于本侧水位线）。身份仍需登记：
        //    迟到事件的重放依然是“同一事件的第二次投递”，不能当作新事件。
        long wm = watermark(side);
        if (event.timestamp() < wm) {
            st.markSeen(side, event);
            metrics.incrementLateDropped();
            return List.of();
        }

        // 3) 缓冲上限检查（水位线停滞时限制缓冲增长）
        int cap = config.maxBufferedPerSide();
        if (cap > 0 && st.bufferedCount(side) >= cap) {
            throw new BufferCapacityExceededException(side, cap);
        }

        st.buffer(side, event);
        st.markSeen(side, event);

        // 4) 与对侧已缓冲事件做区间匹配，全部立即输出（实际产出在 matchAgainst 中计数）
        List<JoinPair> out = new ArrayList<>();
        if (side == StreamSide.LEFT) {
            matchAgainst(event, st.right, true, collector, out);
            // 新左事件若已不可能被任何“未来右事件”匹配（右水位线决定），即时清理，
            // 不必等待下一次右水位线推进。
            st.expireLeft(rightWatermark, config, metrics);
        } else {
            matchAgainst(event, st.left, false, collector, out);
            st.expireRight(leftWatermark, config, metrics);
        }
        if (st.isEmpty()) {
            states.remove(event.key(), st);
        }
        return out;
    }

    /**
     * 推进某一侧的水位线，并据此清理对侧状态与本侧去重记忆。
     *
     * <p>非递增（陈旧）水位线会被忽略并计数。
     */
    public void processWatermark(StreamSide side, long newWatermark) {
        long current = watermark(side);
        if (newWatermark <= current) {
            metrics.incrementStaleWatermark();
            return;
        }
        if (side == StreamSide.LEFT) {
            leftWatermark = newWatermark;
        } else {
            rightWatermark = newWatermark;
        }

        Iterator<Map.Entry<String, KeyedState>> it = states.entrySet().iterator();
        while (it.hasNext()) {
            Map.Entry<String, KeyedState> e = it.next();
            KeyedState st = e.getValue();
            if (side == StreamSide.LEFT) {
                // 左水位线推进 → 右缓冲中“未来左事件也无法匹配”的记录过期
                st.expireRight(newWatermark, config, metrics);
                st.pruneSeen(StreamSide.LEFT, newWatermark);
            } else {
                // 右水位线推进 → 左缓冲中“未来右事件也无法匹配”的记录过期
                st.expireLeft(newWatermark, config, metrics);
                st.pruneSeen(StreamSide.RIGHT, newWatermark);
            }
            if (st.isEmpty()) {
                it.remove();
            }
        }
    }

    /**
     * 用新到达事件扫描对侧缓冲。
     *
     * @param incomingIsLeft 新事件是否来自左侧；决定时间戳窗口方向
     */
    private void matchAgainst(Event incoming,
                              TreeMap<Long, List<Event>> opposite,
                              boolean incomingIsLeft,
                              Collector collector,
                              List<JoinPair> out) {
        long t = incoming.timestamp();
        long lowKey;
        long highKey;
        boolean lowInclusive;
        boolean highInclusive;

        if (incomingIsLeft) {
            // 对侧（右）事件 r 需满足：t + lower <= r.ts <= t + upper
            lowKey = saturatingAdd(t, config.lowerBound());
            highKey = saturatingAdd(t, config.upperBound());
            lowInclusive = config.lowerInclusive();
            highInclusive = config.upperInclusive();
        } else {
            // 对侧（左）事件 l 需满足：l.ts + lower <= t <= l.ts + upper
            // 即 t - upper <= l.ts <= t - lower
            lowKey = saturatingSub(t, config.upperBound());
            highKey = saturatingSub(t, config.lowerBound());
            lowInclusive = config.upperInclusive();
            highInclusive = config.lowerInclusive();
        }

        boolean fullRange = lowKey == Long.MIN_VALUE && highKey == Long.MAX_VALUE;
        NavigableMap<Long, List<Event>> range;
        if (fullRange) {
            range = opposite;
        } else if (lowKey == Long.MIN_VALUE) {
            range = opposite.headMap(highKey, highInclusive);
        } else if (highKey == Long.MAX_VALUE) {
            range = opposite.tailMap(lowKey, lowInclusive);
        } else {
            range = opposite.subMap(lowKey, lowInclusive, highKey, highInclusive);
        }

        for (List<Event> bucket : range.values()) {
            for (Event candidate : bucket) {
                // 二次校验（极值饱和处理后保证正确）
                boolean ok = incomingIsLeft
                        ? config.matches(t, candidate.timestamp())
                        : config.matches(candidate.timestamp(), t);
                if (!ok) {
                    continue;
                }
                Event l = incomingIsLeft ? incoming : candidate;
                Event r = incomingIsLeft ? candidate : incoming;
                JoinPair pair = new JoinPair(l, r);
                out.add(pair);
                metrics.incrementEmitted();
                if (collector != null) {
                    collector.collect(pair);
                }
            }
        }
    }

    /** 饱和加法：溢出时截断到 {@link Long#MAX_VALUE}/{@link Long#MIN_VALUE}。 */
    static long saturatingAdd(long a, long b) {
        long r = a + b;
        // 同号相加结果变号即溢出
        if (((a ^ r) & (b ^ r)) < 0) {
            return a > 0 ? Long.MAX_VALUE : Long.MIN_VALUE;
        }
        return r;
    }

    /** 饱和减法：溢出时截断到 {@link Long#MAX_VALUE}/{@link Long#MIN_VALUE}。 */
    static long saturatingSub(long a, long b) {
        long r = a - b;
        // a 与 b 异号，且结果符号与 a 相反即溢出
        if (((a ^ b) & (a ^ r)) < 0) {
            return a > 0 ? Long.MAX_VALUE : Long.MIN_VALUE;
        }
        return r;
    }

    /** 单个 key 的两侧状态。 */
    private static final class KeyedState {
        final TreeMap<Long, List<Event>> left = new TreeMap<>();
        final TreeMap<Long, List<Event>> right = new TreeMap<>();

        // 去重记忆：ID -> 事件时间戳；seenByTs 仅用于按水位线高效淘汰
        final Map<String, Long> leftSeen = new java.util.HashMap<>();
        final Map<String, Long> rightSeen = new java.util.HashMap<>();
        final TreeMap<Long, Set<String>> leftSeenByTs = new TreeMap<>();
        final TreeMap<Long, Set<String>> rightSeenByTs = new TreeMap<>();

        int leftCount = 0;
        int rightCount = 0;

        int bufferedCount(StreamSide side) {
            return side == StreamSide.LEFT ? leftCount : rightCount;
        }

        boolean isSeen(StreamSide side, String id) {
            return (side == StreamSide.LEFT ? leftSeen : rightSeen).containsKey(id);
        }

        /** 仅进入时间戳缓冲（迟到事件不缓冲）。 */
        void buffer(StreamSide side, Event e) {
            TreeMap<Long, List<Event>> buf = side == StreamSide.LEFT ? left : right;
            buf.computeIfAbsent(e.timestamp(), k -> new ArrayList<>()).add(e);
            if (side == StreamSide.LEFT) {
                leftCount++;
            } else {
                rightCount++;
            }
        }

        /** 登记唯一 ID（含迟到事件：迟到事件的重放仍属重复投递，不产生输出）。 */
        void markSeen(StreamSide side, Event e) {
            Map<String, Long> seen = side == StreamSide.LEFT ? leftSeen : rightSeen;
            TreeMap<Long, Set<String>> seenByTs = side == StreamSide.LEFT ? leftSeenByTs : rightSeenByTs;
            seen.put(e.id(), e.timestamp());
            seenByTs.computeIfAbsent(e.timestamp(), k -> new java.util.HashSet<>()).add(e.id());
        }

        /**
         * 右水位线推进到 wm：左事件 l 不可能再匹配任何未来右事件时删除。
         * 条件：wm - l.ts 越过 upperBound（闭边界用 &gt;，开边界用 &gt;=）。
         * wm 为初始值（MIN_VALUE）时什么都不能过期。
         */
        void expireLeft(long rightWm, JoinConfig cfg, JoinMetrics metrics) {
            if (rightWm == Long.MIN_VALUE) {
                return;
            }
            while (!left.isEmpty()) {
                Map.Entry<Long, List<Event>> first = left.firstEntry();
                long ts = first.getKey();
                long diff = saturatingSub(rightWm, ts);
                boolean expired = cfg.upperInclusive() ? diff > cfg.upperBound()
                                                       : diff >= cfg.upperBound();
                if (!expired) {
                    break;
                }
                left.pollFirstEntry();
                leftCount -= first.getValue().size();
                for (int i = 0; i < first.getValue().size(); i++) {
                    metrics.incrementExpired(StreamSide.LEFT);
                }
                // 注意：不删除去重记忆——过期事件重放仍须视为重复，由 pruneSeen 稍后淘汰
            }
        }

        /**
         * 左水位线推进到 wm：右事件 r 不可能再匹配任何未来左事件时删除。
         * 条件：r.ts - wm 低于 lowerBound（闭边界用 &lt;，开边界用 &lt;=）。
         * wm 为初始值（MIN_VALUE）时什么都不能过期。
         */
        void expireRight(long leftWm, JoinConfig cfg, JoinMetrics metrics) {
            if (leftWm == Long.MIN_VALUE) {
                return;
            }
            while (!right.isEmpty()) {
                Map.Entry<Long, List<Event>> first = right.firstEntry();
                long ts = first.getKey();
                long diff = saturatingSub(ts, leftWm);
                boolean expired = cfg.lowerInclusive() ? diff < cfg.lowerBound()
                                                       : diff <= cfg.lowerBound();
                if (!expired) {
                    break;
                }
                right.pollFirstEntry();
                rightCount -= first.getValue().size();
                for (int i = 0; i < first.getValue().size(); i++) {
                    metrics.incrementExpired(StreamSide.RIGHT);
                }
            }
        }

        /**
         * 淘汰已无意义的去重记忆。必须同时满足：
         * <ol>
         *   <li>事件时间戳严格小于本侧水位线（该事件已是迟到）；</li>
         *   <li>该时间戳的记录已不在对应缓冲中（否则重放会重新入缓冲、重复输出）。</li>
         * </ol>
         */
        void pruneSeen(StreamSide side, long wm) {
            TreeMap<Long, Set<String>> seenByTs =
                    side == StreamSide.LEFT ? leftSeenByTs : rightSeenByTs;
            Map<String, Long> seen = side == StreamSide.LEFT ? leftSeen : rightSeen;
            TreeMap<Long, List<Event>> buf = side == StreamSide.LEFT ? left : right;
            Map.Entry<Long, Set<String>> e;
            while ((e = seenByTs.firstEntry()) != null && e.getKey() < wm) {
                if (buf.containsKey(e.getKey())) {
                    break; // 该时间戳仍有缓冲记录，其 ID 必须保留
                }
                for (String id : e.getValue()) {
                    seen.remove(id);
                }
                seenByTs.pollFirstEntry();
            }
        }

        boolean isEmpty() {
            return leftCount == 0 && rightCount == 0
                    && leftSeen.isEmpty() && rightSeen.isEmpty();
        }
    }
}
