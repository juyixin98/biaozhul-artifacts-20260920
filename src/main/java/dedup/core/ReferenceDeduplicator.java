package dedup.core;

import java.util.HashMap;
import java.util.Map;
import java.util.PriorityQueue;
import java.util.Comparator;

/**
 * <b>小数据精确参考实现</b>（reference oracle）。
 *
 * <p>与 {@link BoundedTombstoneDeduplicator} 的唯一区别：
 * <ul>
 *   <li><b>没有硬容量上限</b>：不会因墓碑数量被迫淘汰（无 {@code forcedEvictionHorizon}），
 *       因此在小数据量下，凡是落在时间承诺窗口内的判定都是“全知”的精确答案；</li>
 *   <li>时间承诺窗口、水位线单调推进、按 eventTime 释放墓碑、窗口内副本延长锚点等
 *       语义与生产算子<b>逐条相同</b>。</li>
 * </ul>
 *
 * <p>差分测试用随机事件流同时喂给两个实现，核对承诺范围内判定一致；
 * 生产算子只在触及硬上限（参考实现永远不会触及）后才允许与参考实现分歧，
 * 且分歧事件必须显式标记为 UNGUARANTEED。
 */
public final class ReferenceDeduplicator {

    private final long allowedLateness;
    private final Map<CompositeKey, Tombstone> tombstones = new HashMap<>();
    private final PriorityQueue<Map.Entry<CompositeKey, Tombstone>> heap;
    private long watermark = Long.MIN_VALUE;
    private boolean watermarkSet;
    private long seqCounter;

    public ReferenceDeduplicator(long allowedLateness) {
        if (allowedLateness < 0) throw new IllegalArgumentException("allowedLateness 不能为负");
        this.allowedLateness = allowedLateness;
        this.heap = new PriorityQueue<>(Comparator
                .comparingLong((Map.Entry<CompositeKey, Tombstone> e) -> e.getValue().eventTime())
                .thenComparingLong(e -> e.getValue().seq()));
    }

    public DedupResult process(Event event) {
        long horizon = watermarkSet ? watermark - allowedLateness : Long.MIN_VALUE;
        boolean inWindow = !watermarkSet || event.eventTime() >= horizon;
        CompositeKey ck = event.compositeKey();

        if (!inWindow) {
            return new DedupResult(
                    DedupResult.Decision.UNGUARANTEED, false, false,
                    event.key(), event.id(), event.eventTime(),
                    watermark, "参考实现：事件晚于水位线承诺窗口");
        }

        Tombstone existing = tombstones.get(ck);
        if (existing == null) {
            seqCounter++;
            Tombstone t = new Tombstone(event.eventTime(), seqCounter, event.payloadHash());
            tombstones.put(ck, t);
            heap.add(Map.entry(ck, t));
            return new DedupResult(
                    DedupResult.Decision.ACCEPT, true, false,
                    event.key(), event.id(), event.eventTime(),
                    watermark, "参考实现：首次事件");
        }

        boolean mismatch = !existing.payloadHash().equals(event.payloadHash());
        // 与有界算子相同的锚点延长语义：窗口内见到更晚 eventTime 的副本则延长墓碑
        if (event.eventTime() > existing.eventTime()) {
            heap.remove(Map.entry(ck, existing));
            Tombstone updated = new Tombstone(event.eventTime(), existing.seq(), existing.payloadHash());
            tombstones.put(ck, updated);
            heap.add(Map.entry(ck, updated));
        }
        return new DedupResult(
                DedupResult.Decision.SUPPRESS, true, mismatch,
                event.key(), event.id(), event.eventTime(),
                watermark, mismatch ? "参考实现：重复且载荷不同" : "参考实现：重复");
    }

    /** 参考实现的水位线同样单调不减，并据此按时间窗口释放墓碑。 */
    public boolean onWatermark(long newWatermark) {
        if (watermarkSet && newWatermark < watermark) return false;
        if (watermarkSet && newWatermark == watermark) return false;
        watermark = newWatermark;
        watermarkSet = true;
        long horizon = watermark - allowedLateness;
        while (!heap.isEmpty() && heap.peek().getValue().eventTime() < horizon) {
            Map.Entry<CompositeKey, Tombstone> e = heap.poll();
            tombstones.remove(e.getKey(), e.getValue());
        }
        return true;
    }

    public long currentWatermark() { return watermark; }
    public boolean watermarkEverSet() { return watermarkSet; }
    public int tombstoneCount() { return tombstones.size(); }
}
