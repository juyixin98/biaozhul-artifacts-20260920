package dedup.core;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.PriorityQueue;

/**
 * 有界墓碑去重算子（参考实现，小数据量、精确语义）。
 *
 * <h3>语义</h3>
 * <ol>
 *   <li><b>承诺范围</b>：当事件满足
 *       {@code eventTime >= watermark - allowedLateness}（尚无水位线时一律在范围内），
 *       且 {@code eventTime > forcedEvictionHorizon}（未被硬上限淘汰覆盖）时，
 *       去重是被承诺的。</li>
 *   <li>承诺范围内首次见到的复合键 → {@link DedupResult.Decision#ACCEPT}，写入墓碑；
 *       再次见到 → {@link DedupResult.Decision#SUPPRESS}，且载荷哈希不同则置 payloadMismatch
 *       （“同 ID 不同载荷”：即使载荷不同也以 ID 为准抑制，并留下可观察标记）。
 *       若副本的 eventTime 晚于墓碑当前锚点（乱序但更晚的副本），锚点延长到该 eventTime：
 *       系统既然在当前窗口内又一次见到了它，就必须继续保留墓碑，直到水位线真正越过
 *       “最后一次在窗口内见到它”的时间，避免窗口内漏去重。</li>
 *   <li>承诺范围外（极迟重复）→ 墓碑可能已被释放，系统无法识别是否为重复，
 *       返回 {@link DedupResult.Decision#UNGUARANTEED} 并透传，{@code dedupGuaranteed=false}。
 *       这些事件<b>不写墓碑</b>：迟到太久的历史事件不应影响当前去重状态。</li>
 * </ol>
 *
 * <h3>有界状态</h3>
 * 墓碑受双重约束：水位线时间窗口（{@code allowedLateness}）推进时释放旧墓碑；
 * 墓碑数量达到 {@code maxTombstones} 硬上限时，强制淘汰最旧的一批（按 eventTime、seq 排序），
 * 同时抬高 {@code forcedEvictionHorizon}，使被覆盖的事件时间区间被诚实标记为“不可保证”。
 */
public final class BoundedTombstoneDeduplicator implements Deduplicator {

    private final long allowedLateness;
    private final int maxTombstones;

    private final Map<CompositeKey, Tombstone> tombstones = new HashMap<>();
    // 最小堆：最旧（eventTime 小、seq 小）的墓碑在堆顶，支持高效的时间/容量淘汰
    private final PriorityQueue<Map.Entry<CompositeKey, Tombstone>> heap;

    private Long watermark;          // null = 从未推进
    private Long forcedHorizon;      // null = 从未发生强制淘汰
    private long seqCounter;
    private final Stats stats = new Stats();

    public BoundedTombstoneDeduplicator(long allowedLateness, int maxTombstones) {
        if (allowedLateness < 0) {
            throw new IllegalArgumentException("allowedLateness 不能为负");
        }
        if (maxTombstones <= 0) {
            throw new IllegalArgumentException("maxTombstones 必须为正");
        }
        this.allowedLateness = allowedLateness;
        this.maxTombstones = maxTombstones;
        this.heap = new PriorityQueue<>(Comparator
                .comparingLong((Map.Entry<CompositeKey, Tombstone> e) -> e.getValue().eventTime())
                .thenComparingLong(e -> e.getValue().seq()));
    }

    @Override
    public DedupResult process(Event event) {
        stats.seen++;
        CompositeKey ck = event.compositeKey();
        String hash = event.payloadHash();

        // 1) 时间承诺窗口（饱和减法，避免 Long.MIN_VALUE 下溢）
        boolean inTimeWindow = watermark == null
                || event.eventTime() >= safeMinus(watermark, allowedLateness);

        // 2) 硬上限强制淘汰覆盖的事件时间区间：该区间内的去重同样无法承诺
        boolean aboveForcedHorizon = forcedHorizon == null || event.eventTime() > forcedHorizon;

        if (!inTimeWindow || !aboveForcedHorizon) {
            // 极迟 / 被强制淘汰覆盖：墓碑不在有界状态内，无法判断是否重复 —— 透传并诚实标记
            stats.unguarded++;
            String reason = !inTimeWindow
                    ? "eventTime 早于水位线承诺窗口下界 (watermark=" + watermark
                      + ", allowedLateness=" + allowedLateness + ")"
                    : "eventTime 不晚于硬上限强制淘汰下界 forcedEvictionHorizon=" + forcedHorizon;
            return new DedupResult(
                    DedupResult.Decision.UNGUARANTEED, false, false,
                    event.key(), event.id(), event.eventTime(),
                    watermark == null ? Long.MIN_VALUE : watermark, reason);
        }

        // 承诺范围内：查墓碑
        Tombstone existing = tombstones.get(ck);
        if (existing == null) {
            // 首次见到 -> 写入墓碑并输出
            seqCounter++;
            Tombstone t = new Tombstone(event.eventTime(), seqCounter, hash);
            tombstones.put(ck, t);
            heap.add(Map.entry(ck, t));
            stats.accepted++;
            stats.activeTombstonesPeak = Math.max(stats.activeTombstonesPeak, tombstones.size());
            enforceCapacity();
            return new DedupResult(
                    DedupResult.Decision.ACCEPT, true, false,
                    event.key(), event.id(), event.eventTime(),
                    watermark == null ? Long.MIN_VALUE : watermark,
                    "首次事件，写入墓碑");
        }

        // 承诺范围内的重复 -> 抑制（ID 是身份；载荷不同也要标记出来）
        boolean mismatch = !existing.payloadHash().equals(hash);
        if (mismatch) {
            stats.payloadMismatch++;
        }
        stats.suppressed++;

        // 关键：在承诺窗口内又见到该 ID（即使其 eventTime 不同/更晚，即乱序迟到副本），
        // 必须把墓碑锚点延长到“观测到的最大 eventTime”。
        // 否则水位线可能在我们刚刚还于窗口内见过它之后就按旧锚点释放墓碑，
        // 造成“承诺范围内漏去重”。堆顶按 (eventTime,seq) 排序，故需移除后重插。
        if (event.eventTime() > existing.eventTime()) {
            heap.remove(Map.entry(ck, existing));
            Tombstone updated = new Tombstone(event.eventTime(), existing.seq(), existing.payloadHash());
            tombstones.put(ck, updated);
            heap.add(Map.entry(ck, updated));
        }

        return new DedupResult(
                DedupResult.Decision.SUPPRESS, true, mismatch,
                event.key(), event.id(), event.eventTime(),
                watermark == null ? Long.MIN_VALUE : watermark,
                mismatch ? "承诺范围内重复，且载荷与首次副本不同" : "承诺范围内重复，载荷一致");
    }

    @Override
    public boolean onWatermark(long newWatermark) {
        if (watermark != null && newWatermark < watermark) {
            // 时钟回退：水位线单调不减，忽略回退（由 ClockFallbackTest 覆盖）
            return false;
        }
        if (watermark != null && newWatermark == watermark) {
            return false;
        }
        watermark = newWatermark;
        evictByTime();
        return true;
    }

    /** 释放 eventTime < watermark - allowedLateness 的墓碑。 */
    private void evictByTime() {
        long horizon = safeMinus(watermark, allowedLateness);
        while (!heap.isEmpty() && heap.peek().getValue().eventTime() < horizon) {
            Map.Entry<CompositeKey, Tombstone> e = heap.poll();
            // map 中的墓碑可能已因强制淘汰被移除；仅当仍是同一 seq 时计数
            if (tombstones.remove(e.getKey(), e.getValue())) {
                stats.timeEvicted++;
            }
        }
    }

    /**
     * 硬上限保护：超过容量时淘汰最旧的一批，直接降到 90% 水位，避免逐个抖动。
     * 被淘汰墓碑覆盖的事件时间上界抬升 forcedEvictionHorizon —— 这之后该时间区间内
     * 再出现同 ID 事件，只能按 UNGUARANTEED 透传。
     */
    private void enforceCapacity() {
        if (tombstones.size() <= maxTombstones) {
            return;
        }
        int target = (int) (maxTombstones * 0.9);
        if (target >= maxTombstones) { // maxTombstones 极小时的保护
            target = maxTombstones - 1;
        }
        long maxEvictedTime = Long.MIN_VALUE;
        int removed = 0;
        while (tombstones.size() > target && !heap.isEmpty()) {
            Map.Entry<CompositeKey, Tombstone> e = heap.poll();
            if (tombstones.remove(e.getKey(), e.getValue())) {
                removed++;
                maxEvictedTime = Math.max(maxEvictedTime, e.getValue().eventTime());
            }
        }
        if (removed > 0) {
            stats.forcedEvicted += removed;
            forcedHorizon = (forcedHorizon == null)
                    ? maxEvictedTime
                    : Math.max(forcedHorizon, maxEvictedTime);
        }
    }

    private static long safeMinus(long a, long b) {
        long r = a - b;
        // 下溢保护：理论下界即 Long.MIN_VALUE
        return b > 0 && a < Long.MIN_VALUE + b ? Long.MIN_VALUE : r;
    }

    @Override public Long currentWatermark() { return watermark; }
    @Override public int activeTombstones() { return tombstones.size(); }
    @Override public long allowedLateness() { return allowedLateness; }
    @Override public int maxTombstones() { return maxTombstones; }
    @Override public Stats stats() { return stats; }

    // ------------------------------------------------------------------
    // 快照（重启恢复）支持
    // ------------------------------------------------------------------

    /** 仅供恢复重建：直接装载内部状态。watermark 为 null 表示从未推进。 */
    public void restore(Long watermark, Long forcedHorizon, long seqCounter, Stats restoredStats,
                 List<Map.Entry<CompositeKey, Tombstone>> entries) {
        this.watermark = watermark;
        this.forcedHorizon = forcedHorizon;
        this.seqCounter = seqCounter;
        copyStats(restoredStats, this.stats);
        this.tombstones.clear();
        this.heap.clear();
        for (Map.Entry<CompositeKey, Tombstone> e : entries) {
            this.tombstones.put(e.getKey(), e.getValue());
            this.heap.add(Map.entry(e.getKey(), e.getValue()));
        }
        this.stats.activeTombstonesPeak =
                Math.max(this.stats.activeTombstonesPeak, this.tombstones.size());
    }

    public java.util.Collection<Map.Entry<CompositeKey, Tombstone>> tombstoneEntries() {
        return tombstones.entrySet();
    }

    public Long forcedHorizon() { return forcedHorizon; }
    public long seqCounterValue() { return seqCounter; }

    static void copyStats(Stats from, Stats to) {
        to.seen = from.seen;
        to.accepted = from.accepted;
        to.suppressed = from.suppressed;
        to.unguarded = from.unguarded;
        to.payloadMismatch = from.payloadMismatch;
        to.timeEvicted = from.timeEvicted;
        to.forcedEvicted = from.forcedEvicted;
        to.activeTombstonesPeak = from.activeTombstonesPeak;
    }
}
