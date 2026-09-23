package com.example.wm.reference;

import com.example.wm.model.Classification;
import com.example.wm.model.LateReason;
import com.example.wm.model.PartitionStatus;
import com.example.wm.model.StreamEvent;
import com.example.wm.model.WatermarkConfig;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 小数据精确参考实现（朴素 oracle）。
 *
 * <p>与 {@code WatermarkCoordinator} 的增量维护不同，本实现保存<b>完整操作日志</b>，
 * 每次查询都从时间起点把日志平铺重放一遍，直接按定义重算一切：
 * <ul>
 *   <li>分区本地水位线 = 该分区全部已见事件的最大事件时间 − B；</li>
 *   <li>有效水位线 = max(本地水位线, 恢复时刻被抬升的基线)；</li>
 *   <li>活跃全局最小 = 对当时所有 ACTIVE 分区取 min；全局水位线是该序列的单调包络；</li>
 *   <li>空闲 = 处理时间 − 最后事件时间 ≥ idleTimeoutMs（含边界）；</li>
 *   <li>迟到 = 事件时间 ≤ 到达时刻的全局水位线（含边界）。</li>
 * </ul>
 *
 * <p>刻意不做任何增量结构，逻辑平铺直白，作为测试中交叉校验的“标准答案”。
 * 复杂度对历史长度 O(n²)，只适合小数据。
 */
public final class ReferenceCoordinator {

    private sealed interface Entry permits EventEntry, PauseEntry, ResumeEntry, TickEntry {
        long timeMs();
    }

    private record EventEntry(long timeMs, String partition, StreamEvent event) implements Entry {
    }

    private record PauseEntry(long timeMs, String partition) implements Entry {
    }

    private record ResumeEntry(long timeMs, String partition) implements Entry {
    }

    private record TickEntry(long timeMs) implements Entry {
    }

    /** 参考实现给出的单条事件判定结果。 */
    public record EventVerdict(long arrivalTimeMs, StreamEvent event,
                               Classification classification, LateReason reason,
                               boolean resumed, Long globalWatermarkAfter,
                               List<String> timedOutAtArrival) {
    }

    /** 一次 tick 的结果：超时分区与 tick 后的全局水位线。 */
    public record TickVerdict(long timeMs, List<String> timedOut, Long globalWatermarkAfter) {
    }

    private static final class RP {
        PartitionStatus status = PartitionStatus.ACTIVE;
        long lastActivityMs = Long.MIN_VALUE;
        long maxEventTimeMs = Long.MIN_VALUE;
        long floor = Long.MIN_VALUE;
        boolean seen;
        long lateCount;
        long seenCount;

        long local(long bound) {
            return seen ? maxEventTimeMs - bound : Long.MIN_VALUE;
        }

        long effective(long bound) {
            return Math.max(local(bound), floor);
        }
    }

    private final WatermarkConfig config;
    private final List<Entry> log = new ArrayList<>();
    private long cursorMs = Long.MIN_VALUE;

    public ReferenceCoordinator(WatermarkConfig config) {
        this.config = config;
    }

    public void setTime(long timeMs) {
        if (timeMs < cursorMs) {
            throw new IllegalArgumentException("time cannot move backwards");
        }
        cursorMs = timeMs;
    }

    public EventVerdict ingest(StreamEvent event) {
        EventEntry entry = new EventEntry(cursorMs, event.partition(), event);
        MutableVerdict mv = new MutableVerdict();
        ReplayResult r = replayWith(entry, (last, late, resumed, part, timedOut) -> {
            if (last instanceof EventEntry) {
                mv.classification = late ? Classification.LATE : Classification.ON_TIME;
                mv.reason = (late && resumed) ? LateReason.RECOVERED_OLD
                        : (late ? LateReason.NORMAL : null);
                mv.resumed = resumed;
                mv.timedOut = timedOut;
            }
        });
        return new EventVerdict(entry.timeMs(), entry.event(), mv.classification, mv.reason,
                mv.resumed, r.globalAfter, mv.timedOut);
    }

    public void pause(String partition) {
        replayWith(new PauseEntry(cursorMs, partition), null);
    }

    public Long resume(String partition) {
        return replayWith(new ResumeEntry(cursorMs, partition), null).globalAfter;
    }

    public TickVerdict tick() {
        TickEntry entry = new TickEntry(cursorMs);
        MutableTick mt = new MutableTick();
        ReplayResult r = replayWith(entry, (last, late, resumed, part, timedOut) -> {
            if (last instanceof TickEntry) {
                mt.timedOut = timedOut;
            }
        });
        return new TickVerdict(entry.timeMs(), List.copyOf(mt.timedOut), r.globalAfter);
    }
    // -- 结果访问 ----------------------------------------------------------------

    public Long globalWatermark() {
        return replay(log).globalAfter;
    }

    public PartitionStatus statusOf(String partition) {
        ReplayResult r = replay(log);
        RP p = r.parts.get(partition);
        return p == null ? null : p.status;
    }

    public Long effectiveWatermark(String partition) {
        ReplayResult r = replay(log);
        RP p = r.parts.get(partition);
        return (p == null || !p.seen) ? null : p.effective(config.outOfOrdernessBoundMs());
    }

    public Long localWatermark(String partition) {
        ReplayResult r = replay(log);
        RP p = r.parts.get(partition);
        return (p == null || !p.seen) ? null : p.local(config.outOfOrdernessBoundMs());
    }

    public Long maxEventTime(String partition) {
        RP p = replay(log).parts.get(partition);
        return (p == null || !p.seen) ? null : p.maxEventTimeMs;
    }

    public long seenCount(String partition) {
        RP p = replay(log).parts.get(partition);
        return p == null ? 0 : p.seenCount;
    }

    public long lateCount(String partition) {
        RP p = replay(log).parts.get(partition);
        return p == null ? 0 : p.lateCount;
    }

    public long totalLateCount() {
        return replay(log).parts.values().stream().mapToLong(p -> p.lateCount).sum();
    }

    // -- 朴素重放 ----------------------------------------------------------------

    private record ReplayResult(Map<String, RP> parts, Long globalAfter) {
    }

    private static final class MutableVerdict {
        Classification classification;
        LateReason reason;
        boolean resumed;
        List<String> timedOut = List.of();
    }

    private static final class MutableTick {
        List<String> timedOut = List.of();
    }

    /** 标记“最后一个日志条目”的判定接收器。 */
    private interface LastEntrySink {
        void accept(Entry lastEntry, boolean late, boolean resumed, RP part, List<String> timedOut);
    }

    /**
     * 把 hypothetical 追加到日志末尾（若为 null 则只重放），再从空状态开始
     * 完整扫描整个日志。返回重放结束时的状态。
     */
    private ReplayResult replayWith(Entry hypothetical, LastEntrySink sink) {
        List<Entry> all = new ArrayList<>(log);
        if (hypothetical != null) {
            all.add(hypothetical);
        }
        ReplayResult r = replay(all, sink);
        if (hypothetical != null) {
            log.add(hypothetical);
        }
        return r;
    }

    private ReplayResult replay(List<Entry> entries) {
        return replay(entries == null ? List.of() : entries, null);
    }

    private ReplayResult replay(List<Entry> entries, LastEntrySink sink) {
        Map<String, RP> parts = new LinkedHashMap<>();
        Long global = null;
        long bound = config.outOfOrdernessBoundMs();
        long timeout = config.idleTimeoutMs();
        Entry lastEntry = entries.isEmpty() ? null : lastOf(entries);

        for (Entry e : entries) {
            long now = e.timeMs();

            // 1) 先做空闲超时检测（对所有 ACTIVE 且收过事件的分区）。
            List<String> timedOut = new ArrayList<>();
            for (RP p : parts.values()) {
                if (p.status == PartitionStatus.ACTIVE && p.seen
                        && timeout != WatermarkConfig.NO_IDLE_TIMEOUT
                        && now - p.lastActivityMs >= timeout) {
                    p.status = PartitionStatus.IDLE;
                    timedOut.add(nameOf(parts, p));
                }
            }

            RP p;
            if (e instanceof TickEntry) {
                p = null;
            } else {
                p = switch (e) {
                    case EventEntry ee -> parts.computeIfAbsent(ee.partition(), k -> new RP());
                    case PauseEntry pe -> parts.computeIfAbsent(pe.partition(), k -> new RP());
                    case ResumeEntry re -> parts.computeIfAbsent(re.partition(), k -> new RP());
                    default -> throw new IllegalStateException("unreachable");
                };
            }
            boolean resumed = false;

            // 与主引擎一致：超时检测后立即取一次单调包络，再处理本条目，
            // 保证“同一时刻某分区超时、某分区恢复”时恢复基线读到的是抬升后的全局值。
            global = advanceEnvelope(parts, global, bound);

            if (e instanceof PauseEntry) {
                p.status = PartitionStatus.PAUSED;
            } else if (e instanceof ResumeEntry) {
                if (p.status != PartitionStatus.ACTIVE) {
                    resumed = true;
                    p.status = PartitionStatus.ACTIVE;
                    if (global != null) {
                        p.floor = Math.max(p.floor, global);
                    }
                }
            } else if (e instanceof EventEntry ee) {
                if (p.status != PartitionStatus.ACTIVE) {
                    resumed = true;
                    p.status = PartitionStatus.ACTIVE;
                    if (global != null) {
                        p.floor = Math.max(p.floor, global);
                    }
                }
                long t = ee.event.eventTimeMs();
                boolean late = global != null && t <= global;
                boolean wasAlreadySeen = p.seen;
                if (sink != null && e == lastEntry) {
                    sink.accept(e, late, resumed && wasAlreadySeen, p, List.copyOf(timedOut));
                }
                if (late) {
                    p.lateCount++;
                }
                p.seenCount++;
                p.seen = true;
                p.lastActivityMs = now;
                p.maxEventTimeMs = Math.max(p.maxEventTimeMs, t);
            } else if (e instanceof TickEntry) {
                if (sink != null && e == lastEntry) {
                    sink.accept(e, false, false, null, List.copyOf(timedOut));
                }
            }

            // 2) 按定义重算当前 min，再取单调包络。
            global = advanceEnvelope(parts, global, bound);
        }
        return new ReplayResult(parts, global);
    }

    /** 对所有 ACTIVE 且收过事件的分区取有效水位线最小值，推进（绝不拉低）全局水位线。 */
    private Long advanceEnvelope(Map<String, RP> parts, Long global, long bound) {
        Long min = null;
        for (RP q : parts.values()) {
            if (q.status == PartitionStatus.ACTIVE && q.seen) {
                long w = q.effective(bound);
                if (min == null || w < min) {
                    min = w;
                }
            }
        }
        if (min != null && (global == null || min > global)) {
            return min;
        }
        return global;
    }

    private static Entry lastOf(List<Entry> entries) {
        return entries.get(entries.size() - 1);
    }

    private static String nameOf(Map<String, RP> parts, RP target) {
        for (Map.Entry<String, RP> e : parts.entrySet()) {
            if (e.getValue() == target) {
                return e.getKey();
            }
        }
        throw new IllegalStateException("partition not found");
    }
}
