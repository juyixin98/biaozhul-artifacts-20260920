package com.example.wm.engine;

import com.example.wm.model.Classification;
import com.example.wm.model.CoordinationSnapshot;
import com.example.wm.model.IngestionResult;
import com.example.wm.model.LateEvent;
import com.example.wm.model.LateReason;
import com.example.wm.model.PartitionStateView;
import com.example.wm.model.PartitionStatus;
import com.example.wm.model.StreamEvent;
import com.example.wm.model.TickResult;
import com.example.wm.model.WatermarkConfig;
import com.example.wm.time.Clock;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * 多源（多分区）水位线协调器。
 *
 * <h2>水位线规则</h2>
 * <ul>
 *   <li>分区 p 的本地水位线：{@code wmLocal(p) = maxEventTime(p) − B}，B 为乱序等待宽度。</li>
 *   <li>全局水位线：{@code min(wmEffective(p))}，仅对 {@link PartitionStatus#ACTIVE} 分区取最小。</li>
 *   <li>全局水位线在时间上单调不减；没有任何活跃分区时为 {@code null}（尚未定义）。</li>
 * </ul>
 *
 * <h2>空闲检测</h2>
 * 分区自最后一次事件起，处理时间经过 {@code idleTimeoutMs} 仍无新事件，则在下次
 * {@link #tick()} 或任意事件注入时转为 {@link PartitionStatus#IDLE}，退出 min 聚合，
 * 不再拖住全局水位线。判定边界为“含等于”：{@code now − lastEventTimeMs ≥ timeout} 即超时。
 *
 * <h2>暂停 / 恢复与全局水位线不倒退</h2>
 * {@link #pausePartition} 显式暂停分区，语义与 IDLE 相同（退出 min 聚合）。
 * 分区恢复（重新注入事件）时，其有效水位线基线被抬升到“恢复时刻的全局水位线”：
 * <pre>wmEffective(p) = max(wmLocal(p), globalWatermarkAtResume)</pre>
 * 因此恢复分区永远不会把全局水位线拉低——它必须追上全局进度后才重新参与 min 聚合。
 * 注意：抬高的只是“参与聚合的有效基线”，{@code wmLocal} 仍如实反映该分区数据，
 * 快照同时暴露二者。
 *
 * <h2>迟到判定</h2>
 * 全局水位线 w 断言“事件时间 ≤ w 的事件都已到齐”，故事件时间戳 t 满足
 * {@code t ≤ w} 即进入迟到通道（边界含等于）。恢复分区重放的旧事件额外标记
 * {@link LateReason#RECOVERED_OLD}。判定相对的是“全局”水位线，因此一个落后分区
 * 即使自身本地水位线更低，也不会错误接收已经过全局截止时间的旧事件。
 */
public final class WatermarkCoordinator {

    /** 迟到事件监听器，便于把迟到事件旁路到外部通道（不影响引擎内部记录）。 */
    @FunctionalInterface
    public interface LateEventListener {
        void onLateEvent(LateEvent event);
    }

    private static final class Partition {
        final String name;
        PartitionStatus status = PartitionStatus.ACTIVE;
        long lastEventTimeMs;            // 仅在 seenEvents>0 时有效
        long maxEventTimeMs = Long.MIN_VALUE;
        long watermarkFloor = Long.MIN_VALUE; // 恢复时抬升的有效水位线基线
        long seenEvents;
        long lateEvents;
        boolean everSeen;

        Partition(String name) {
            this.name = name;
        }

        long localWatermark(long bound) {
            return everSeen ? maxEventTimeMs - bound : Long.MIN_VALUE;
        }

        long effectiveWatermark(long bound) {
            return Math.max(localWatermark(bound), watermarkFloor);
        }
    }

    private final WatermarkConfig config;
    private final Clock clock;
    private final Map<String, Partition> partitions = new LinkedHashMap<>();
    private final List<LateEvent> lateEvents = new ArrayList<>();
    private final List<LateEventListener> listeners = new CopyOnWriteArrayList<>();
    private Long globalWatermark; // null = 尚未定义（无活跃分区）

    private long tickCount;

    public WatermarkCoordinator(WatermarkConfig config, Clock clock) {
        this.config = config;
        this.clock = clock;
    }

    public WatermarkConfig config() {
        return config;
    }

    public void addLateEventListener(LateEventListener listener) {
        listeners.add(listener);
    }

    /** 预注册分区：新分区以 ACTIVE 身份进入聚合（尚无水位线则不约束 min）。 */
    public synchronized void registerPartition(String name) {
        partitions.computeIfAbsent(name, Partition::new);
    }

    // ------------------------------------------------------------------
    // 事件注入
    // ------------------------------------------------------------------

    /**
     * 注入一条事件。若该分区此前处于 IDLE/PAUSED，则自动恢复为 ACTIVE。
     */
    public synchronized IngestionResult ingest(StreamEvent event) {
        long now = clock.currentTimeMillis();
        Long previousGlobal = globalWatermark;

        List<String> timedOut = detectTimeouts(now);

        Partition p = partitions.computeIfAbsent(event.partition(), Partition::new);
        boolean wasInactive = p.status != PartitionStatus.ACTIVE;
        boolean hadSeenEvents = p.everSeen;
        if (wasInactive) {
            // 恢复：把有效水位线基线抬到当前全局水位线，保证全局不倒退。
            // 全新分区（从未收过事件）基线保持 MIN_VALUE。
            p.status = PartitionStatus.ACTIVE;
            if (globalWatermark != null) {
                p.watermarkFloor = Math.max(p.watermarkFloor, globalWatermark);
            }
        }
        // “恢复”仅用于此前收过事件、中途空闲/暂停后重新接入的分区；
        // 从未见过数据的分区第一次到事件不算恢复。
        boolean resumed = wasInactive && hadSeenEvents;

        Classification classification;
        LateReason reason = null;
        boolean late = globalWatermark != null && event.eventTimeMs() <= globalWatermark;
        if (late) {
            classification = Classification.LATE;
            // 恢复分区重放的、且真正过了全局截止时间的旧事件：恢复旧事件
            reason = resumed ? LateReason.RECOVERED_OLD : LateReason.NORMAL;
            p.lateEvents++;
            LateEvent le = new LateEvent(event, reason, globalWatermark, now);
            lateEvents.add(le);
            for (LateEventListener l : listeners) {
                l.onLateEvent(le);
            }
        } else {
            classification = Classification.ON_TIME;
        }

        // 无论准时还是迟到都更新观察值（迟到事件携带的最大时间戳也纳入水位线进度，
        // 与“watermark = max(eventTime) − B”的数据流式定义一致）。
        p.seenEvents++;
        p.everSeen = true;
        p.lastEventTimeMs = now;
        if (event.eventTimeMs() > p.maxEventTimeMs) {
            p.maxEventTimeMs = event.eventTimeMs();
        }

        recomputeGlobalWatermark();
        return new IngestionResult(!late, classification, late ? reason : null, resumed,
                previousGlobal, globalWatermark, List.copyOf(timedOut));
    }

    // ------------------------------------------------------------------
    // 显式暂停 / 恢复
    // ------------------------------------------------------------------

    /** 显式暂停分区：退出 min 聚合。不存在时先注册再暂停。 */
    public synchronized void pausePartition(String name) {
        detectTimeouts(clock.currentTimeMillis());
        Partition p = partitions.computeIfAbsent(name, Partition::new);
        p.status = PartitionStatus.PAUSED;
        recomputeGlobalWatermark();
    }

    /**
     * 显式恢复分区（无事件）。与注入事件恢复使用同一条“基线抬升”规则，
     * 返回该分区恢复后的有效水位线（尚未收到过事件为 null）。
     */
    public synchronized Long resumePartition(String name) {
        detectTimeouts(clock.currentTimeMillis());
        Partition p = partitions.computeIfAbsent(name, Partition::new);
        boolean wasInactive = p.status != PartitionStatus.ACTIVE;
        p.status = PartitionStatus.ACTIVE;
        if (wasInactive && globalWatermark != null) {
            p.watermarkFloor = Math.max(p.watermarkFloor, globalWatermark);
        }
        recomputeGlobalWatermark();
        return p.everSeen ? p.effectiveWatermark(config.outOfOrdernessBoundMs()) : null;
    }

    /**
     * 时钟推进：执行空闲超时检测并重算全局水位线。
     * 测试中通常由 {@code Scheduler} 周期调用，也可手动调用。
     */
    public synchronized TickResult tick() {
        long now = clock.currentTimeMillis();
        tickCount++;
        Long previousGlobal = globalWatermark;
        List<String> timedOut = detectTimeouts(now);
        recomputeGlobalWatermark();
        return new TickResult(now, List.copyOf(timedOut), previousGlobal, globalWatermark);
    }

    // ------------------------------------------------------------------
    // 内部规则
    // ------------------------------------------------------------------

    private List<String> detectTimeouts(long now) {
        if (config.idleTimeoutMs() == WatermarkConfig.NO_IDLE_TIMEOUT) {
            return List.of();
        }
        List<String> timedOut = new ArrayList<>();
        for (Partition p : partitions.values()) {
            if (p.status == PartitionStatus.ACTIVE && p.everSeen
                    && now - p.lastEventTimeMs >= config.idleTimeoutMs()) {
                p.status = PartitionStatus.IDLE;
                timedOut.add(p.name);
            }
        }
        if (!timedOut.isEmpty()) {
            recomputeGlobalWatermark();
        }
        return timedOut;
    }

    private void recomputeGlobalWatermark() {
        Long min = null;
        for (Partition p : partitions.values()) {
            if (p.status == PartitionStatus.ACTIVE && p.everSeen) {
                long w = p.effectiveWatermark(config.outOfOrdernessBoundMs());
                if (min == null || w < min) {
                    min = w;
                }
            }
        }
        // 关键不变量：全局水位线单调不减。
        if (min != null && (globalWatermark == null || min > globalWatermark)) {
            globalWatermark = min;
        }
        // min == null（无活跃分区）时保留既有 globalWatermark：
        // 全部空闲/暂停不应把已经推进的水位线“清空”，快照通过 activeCount 表达该状态。
    }

    // ------------------------------------------------------------------
    // 查询
    // ------------------------------------------------------------------

    public synchronized Long globalWatermark() {
        return globalWatermark;
    }

    public synchronized long processingTimeMs() {
        return clock.currentTimeMillis();
    }

    public synchronized List<LateEvent> lateEvents() {
        return List.copyOf(lateEvents);
    }

    public synchronized CoordinationSnapshot snapshot() {
        List<PartitionStateView> views = new ArrayList<>();
        int active = 0, idle = 0, paused = 0;
        for (Partition p : partitions.values()) {
            if (p.status == PartitionStatus.ACTIVE) active++;
            else if (p.status == PartitionStatus.IDLE) idle++;
            else paused++;
            Long maxTs = p.everSeen ? p.maxEventTimeMs : null;
            Long local = p.everSeen ? p.localWatermark(config.outOfOrdernessBoundMs()) : null;
            Long eff = p.everSeen ? p.effectiveWatermark(config.outOfOrdernessBoundMs()) : null;
            Long last = p.everSeen ? p.lastEventTimeMs : null;
            views.add(new PartitionStateView(p.name, p.status, last, maxTs, local, eff,
                    p.seenEvents, p.lateEvents));
        }
        views.sort(Comparator.comparing(PartitionStateView::partition));
        return new CoordinationSnapshot(clock.currentTimeMillis(), globalWatermark,
                active, idle, paused, List.copyOf(views), lateEvents.size());
    }

    synchronized long tickCount() {
        return tickCount;
    }
}
