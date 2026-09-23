package com.tjoin.core;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 小数据精确参考实现（reference oracle）。
 *
 * <p>朴素、显而易见地正确：保存<b>所有已接纳（非重复、非迟到）</b>事件，
 * 每次用 O(n²) 暴力比较，去重后输出全部满足区间条件的配对。不做任何提前清理，
 * 也不依赖水位线驱动的状态淘汰，因此可作为流式算子的对照基准。
 *
 * <p>语义约定（与 {@link IntervalJoinOperator} 对齐）：
 * <ul>
 *   <li>同一侧、同一事件 ID 的重复投递忽略；</li>
 *   <li>事件时间戳小于本侧<b>当前</b>水位线的事件视为迟到，忽略；
 *       参考实现按相同的喂入顺序驱动，二者对“迟到”的判定一致；</li>
 *   <li>不同 key 的事件不连接；</li>
 *   <li>每个 (leftId, rightId) 配对恰好输出一次，输出顺序为
 *       “配对第二次到达时”的发现顺序，与流式算子相同。</li>
 * </ul>
 */
public final class ReferenceIntervalJoin {

    private final JoinConfig config;
    private final Map<String, List<Event>> leftByKey = new LinkedHashMap<>();
    private final Map<String, List<Event>> rightByKey = new LinkedHashMap<>();
    private final java.util.Set<String> leftIds = new java.util.HashSet<>();
    private final java.util.Set<String> rightIds = new java.util.HashSet<>();
    private final java.util.Set<String> emittedKeys = new java.util.HashSet<>();

    private long leftWatermark = IntervalJoinOperator.INITIAL_WATERMARK;
    private long rightWatermark = IntervalJoinOperator.INITIAL_WATERMARK;

    public ReferenceIntervalJoin(JoinConfig config) {
        this.config = config;
    }

    /**
     * 喂入一个事件，返回它与先前事件形成的新配对。
     * 重复/迟到事件返回空列表，且迟到事件不进入状态（与流式算子一致）。
     */
    public List<JoinPair> processEvent(Event e) {
        StreamSide side = e.side();
        long wm = side == StreamSide.LEFT ? leftWatermark : rightWatermark;
        java.util.Set<String> ownIds = side == StreamSide.LEFT ? leftIds : rightIds;
        if (!ownIds.add(e.id())) {
            return List.of(); // 重复投递
        }
        if (e.timestamp() < wm) {
            return List.of(); // 迟到：丢弃，不进入状态
        }

        List<JoinPair> out = new ArrayList<>();
        if (side == StreamSide.LEFT) {
            for (Event r : rightByKey.getOrDefault(e.key(), List.of())) {
                emitIfMatch(e, r, out);
            }
            leftByKey.computeIfAbsent(e.key(), k -> new ArrayList<>()).add(e);
        } else {
            for (Event l : leftByKey.getOrDefault(e.key(), List.of())) {
                emitIfMatch(l, e, out);
            }
            rightByKey.computeIfAbsent(e.key(), k -> new ArrayList<>()).add(e);
        }
        return out;
    }

    /** 参考实现同样跟踪水位线（仅用于与流式实现一致地判定迟到）。 */
    public void processWatermark(StreamSide side, long wm) {
        if (side == StreamSide.LEFT) {
            leftWatermark = Math.max(leftWatermark, wm);
        } else {
            rightWatermark = Math.max(rightWatermark, wm);
        }
    }

    private void emitIfMatch(Event l, Event r, List<JoinPair> out) {
        if (!l.key().equals(r.key())) {
            return;
        }
        if (!config.matches(l.timestamp(), r.timestamp())) {
            return;
        }
        String pairKey = l.id() + "\u0000" + r.id();
        if (!emittedKeys.add(pairKey)) {
            return; // 防御性去重：配对级唯一
        }
        out.add(new JoinPair(l, r));
    }
}
