package dedup.stream;

import dedup.core.BoundedTombstoneDeduplicator;
import dedup.core.Deduplicator;
import dedup.core.DedupResult;
import dedup.core.Event;
import dedup.core.Stats;
import dedup.json.Json;
import dedup.state.SnapshotData;
import dedup.state.SnapshotStore;
import dedup.time.Clock;
import dedup.watermark.BoundedOutOfOrdernessWatermarks;
import dedup.watermark.ManualWatermarkGenerator;
import dedup.watermark.WatermarkGenerator;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * 流处理编排器（线程安全）：把去重算子、水位线生成器、有界输出缓冲与快照持久化组装在一起。
 *
 * <p>所有公共方法 synchronized：小数据量演示/验收场景，简单的串行化优先于吞吐。
 */
public final class StreamProcessor {

    /** 处理配置。 */
    public record Config(long allowedLateness, int maxTombstones, String watermarkMode,
                         long outOfOrderness, int maxBufferedOutputs) {

        public Config {
            if (allowedLateness < 0) throw new IllegalArgumentException("allowedLateness 不能为负");
            if (maxTombstones <= 0) throw new IllegalArgumentException("maxTombstones 必须为正");
            if (maxBufferedOutputs < 1) throw new IllegalArgumentException("maxBufferedOutputs 必须为正");
            if (!"manual".equals(watermarkMode) && !"bounded".equals(watermarkMode)) {
                throw new IllegalArgumentException("watermarkMode 必须是 manual 或 bounded");
            }
            if (outOfOrderness < 0) throw new IllegalArgumentException("outOfOrderness 不能为负");
        }

        public static Config defaults() {
            return new Config(5_000, 100_000, "manual", 2_000, 10_000);
        }
    }

    private final Config config;
    private final Clock clock;
    private final SnapshotStore store; // null = 不持久化
    private final BoundedTombstoneDeduplicator dedup;
    private final WatermarkGenerator watermarkGenerator;

    private final ArrayDeque<OutputEntry> outputBuffer = new ArrayDeque<>();
    private long outputSeq;
    private long droppedOutputs;

    private StreamProcessor(Config config, Clock clock, SnapshotStore store,
                            WatermarkGenerator generator) {
        this.config = config;
        this.clock = clock;
        this.store = store;
        this.watermarkGenerator = generator;
        this.dedup = new BoundedTombstoneDeduplicator(
                config.allowedLateness(), config.maxTombstones());
    }

    /** 创建处理器：若 store 中存在快照则自动恢复（重启恢复入口）。 */
    public static StreamProcessor create(Config config, Clock clock, SnapshotStore store) {
        WatermarkGenerator generator = "bounded".equals(config.watermarkMode())
                ? new BoundedOutOfOrdernessWatermarks(config.outOfOrderness())
                : new ManualWatermarkGenerator();
        StreamProcessor p = new StreamProcessor(config, clock, store, generator);
        p.restoreIfPresent();
        return p;
    }

    /** 测试便捷构造：自定义生成器、不持久化。 */
    public static StreamProcessor withGenerator(Config config, Clock clock,
                                                WatermarkGenerator generator) {
        return new StreamProcessor(config, clock, null, generator);
    }

    // ------------------------------------------------------------------
    // 处理
    // ------------------------------------------------------------------

    /** 处理一个事件（先更新水位线生成器内部观测，再在当前水位线下做去重判定）。 */
    public synchronized DedupResult process(Event event) {
        Long advance = watermarkGenerator.onEvent(event);
        if (advance != null) {
            applyWatermark(advance);
        }
        DedupResult result = dedup.process(event);
        if (result.emitted()) {
            outputSeq++;
            outputBuffer.addLast(new OutputEntry(outputSeq, event, result));
            while (outputBuffer.size() > config.maxBufferedOutputs()) {
                outputBuffer.pollFirst();
                droppedOutputs++;
            }
        }
        return result;
    }

    /** 周期触发（由注入的调度器调用）：bounded 模式据此发出水位线。 */
    public synchronized Long tick() {
        Long candidate = watermarkGenerator.onPeriodicTick(clock.nowMillis());
        if (candidate != null && applyWatermark(candidate)) {
            return candidate;
        }
        return null;
    }

    /** manual 模式显式推进水位线；回退返回 false（时钟回退被拒绝）。 */
    public synchronized boolean setManualWatermark(long newWatermark) {
        if (!(watermarkGenerator instanceof ManualWatermarkGenerator m)) {
            throw new IllegalStateException("当前 watermarkMode=" + config.watermarkMode()
                    + "，水位线由事件/周期自动推进，请使用 /tick");
        }
        if (!m.setWatermark(newWatermark)) {
            return false;
        }
        boolean advanced = dedup.onWatermark(newWatermark);
        if (advanced) {
            checkpoint();
        }
        return advanced;
    }

    private boolean applyWatermark(long newWatermark) {
        boolean advanced = dedup.onWatermark(newWatermark);
        if (advanced) {
            checkpoint(); // 每次水位线前进都落盘，重启后状态确定
        }
        return advanced;
    }

    // ------------------------------------------------------------------
    // 输出缓冲
    // ------------------------------------------------------------------

    /** 取出 seq 严格大于 sinceSeq 的已发射事件；drain=true 时同时把它们移出缓冲区。 */
    public synchronized List<OutputEntry> drainOutputs(long sinceSeq, boolean drain) {
        List<OutputEntry> out = new ArrayList<>();
        var it = outputBuffer.iterator();
        while (it.hasNext()) {
            OutputEntry e = it.next();
            if (e.seq() > sinceSeq) {
                out.add(e);
                if (drain) it.remove();
            }
        }
        return out;
    }

    // ------------------------------------------------------------------
    // 快照
    // ------------------------------------------------------------------

    /** 手动触发检查点（水位线之外的恢复点，例如关闭前）。 */
    public synchronized void checkpoint() {
        if (store == null) return;
        try {
            SnapshotData snap = SnapshotData.capture(
                    dedup, watermarkGenerator, config.watermarkMode(), clock.nowMillis());
            store.save(snap);
        } catch (Exception e) {
            throw new RuntimeException("快照保存失败: " + e.getMessage(), e);
        }
    }

    private void restoreIfPresent() {
        if (store == null) return;
        try {
            if (!store.exists()) return;
            SnapshotData snap = store.load();
            if (snap == null) return;

            Stats restoredStats = snap.restoreStats();
            List<Map.Entry<dedup.core.CompositeKey, dedup.core.Tombstone>> entries =
                    snap.restoreEntries();
            dedup.restore(snap.watermark(), snap.forcedHorizon(), snap.seqCounter(),
                    restoredStats, entries);
            snap.restoreGenerator(watermarkGenerator);
        } catch (Exception e) {
            throw new RuntimeException("快照恢复失败: " + e.getMessage(), e);
        }
    }

    // ------------------------------------------------------------------
    // 可观察状态
    // ------------------------------------------------------------------

    public synchronized Json.Obj statsJson() {
        Json.Obj o = dedup.stats().toJson();
        o.put("bufferedOutputs", Json.Num.of(outputBuffer.size()));
        o.put("droppedOutputs", Json.Num.of(droppedOutputs));
        o.put("emittedTotal", Json.Num.of(outputSeq));
        o.put("activeTombstones", Json.Num.of(dedup.activeTombstones()));
        o.put("maxTombstones", Json.Num.of(dedup.maxTombstones()));
        o.put("allowedLateness", Json.Num.of(dedup.allowedLateness()));
        o.put("watermarkMode", new Json.Str(config.watermarkMode()));
        Long wm = dedup.currentWatermark();
        o.put("watermark", wm == null ? Json.Nul.INSTANCE : Json.Num.of(wm));
        Long horizon = dedup.forcedHorizon();
        o.put("forcedEvictionHorizon",
                horizon == null ? Json.Nul.INSTANCE : Json.Num.of(horizon));
        return o;
    }

    public synchronized void reset() {
        outputBuffer.clear();
        outputSeq = 0;
        droppedOutputs = 0;
        // 去重器内部状态清空：用空状态重新装载
        dedup.restore(null, null, 0L, new Stats(), List.of());
        if (watermarkGenerator instanceof ManualWatermarkGenerator m) {
            m.reset();
        } else if (watermarkGenerator instanceof BoundedOutOfOrdernessWatermarks b) {
            b.restore(Long.MIN_VALUE, null);
        }
        if (store != null) {
            try {
                store.clear();
            } catch (Exception e) {
                throw new RuntimeException("清除快照失败", e);
            }
        }
    }

    public Deduplicator deduplicator() { return dedup; }

    public Config config() { return config; }
}
