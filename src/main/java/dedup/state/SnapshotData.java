package dedup.state;

import dedup.core.BoundedTombstoneDeduplicator;
import dedup.core.CompositeKey;
import dedup.core.Stats;
import dedup.core.Tombstone;
import dedup.json.Json;
import dedup.watermark.BoundedOutOfOrdernessWatermarks;
import dedup.watermark.ManualWatermarkGenerator;
import dedup.watermark.WatermarkGenerator;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * 快照内容（序列化为 JSON 原子落盘）。
 * 包含去重算子的全部墓碑与计数器、序列号、强制淘汰下界，以及水位线生成器状态。
 * 序列号使用重启前的值，保证恢复后墓碑顺序仍与历史一致。
 */
public record SnapshotData(
        long version,
        long savedAtMillis,
        Long watermark,
        Long forcedHorizon,
        long seqCounter,
        String watermarkMode,
        Long genMaxTimestamp,
        List<Json.Obj> tombstones,
        Json.Obj stats) {

    public static final long FORMAT_VERSION = 1;

    public static SnapshotData capture(
            BoundedTombstoneDeduplicator dedup,
            WatermarkGenerator generator,
            String watermarkMode,
            long nowMillis) {

        List<Json.Obj> entries = new ArrayList<>();
        for (Map.Entry<CompositeKey, Tombstone> e : dedup.tombstoneEntries()) {
            Json.Obj o = e.getValue().toJson(e.getKey().key(), e.getKey().id());
            entries.add(o);
        }

        Long genMax = null;
        if (generator instanceof BoundedOutOfOrdernessWatermarks b) {
            genMax = b.maxTimestampValue();
        }
        return new SnapshotData(
                FORMAT_VERSION, nowMillis,
                dedup.currentWatermark(), dedup.forcedHorizon(), dedup.seqCounterValue(),
                watermarkMode, genMax, entries, dedup.stats().toJson());
    }

    public Json.Obj toJson() {
        Json.Obj o = new Json.Obj();
        o.put("formatVersion", Json.Num.of(version));
        o.put("savedAtMillis", Json.Num.of(savedAtMillis));
        o.put("watermark", watermark == null ? Json.Nul.INSTANCE : Json.Num.of(watermark));
        o.put("forcedHorizon", forcedHorizon == null ? Json.Nul.INSTANCE : Json.Num.of(forcedHorizon));
        o.put("seqCounter", Json.Num.of(seqCounter));
        Json.Obj wm = new Json.Obj();
        wm.put("mode", new Json.Str(watermarkMode));
        wm.put("maxTimestamp", genMaxTimestamp == null ? Json.Nul.INSTANCE : Json.Num.of(genMaxTimestamp));
        o.put("watermarkGenerator", wm);
        Json.Arr arr = new Json.Arr();
        arr.addAll(tombstones);
        o.put("tombstones", arr);
        o.put("stats", stats);
        return o;
    }

    @SuppressWarnings("unchecked")
    public static SnapshotData fromJson(Json.Value v) {
        if (!(v instanceof Json.Obj o)) {
            throw new Json.JsonException("快照根必须是 JSON 对象");
        }
        long version = Json.longExact(o.get("formatVersion"), "formatVersion");
        long savedAt = Json.longExact(o.get("savedAtMillis"), "savedAtMillis");
        Long wm = o.get("watermark") instanceof Json.Num n ? n.value().longValueExact() : null;
        Long fh = o.get("forcedHorizon") instanceof Json.Num n ? n.value().longValueExact() : null;
        long seq = Json.longExact(o.get("seqCounter"), "seqCounter");

        Json.Obj wmObj = o.get("watermarkGenerator") instanceof Json.Obj w ? w : new Json.Obj();
        String mode = wmObj.getStr("mode") == null ? "manual" : wmObj.getStr("mode");
        Long genMax = wmObj.get("maxTimestamp") instanceof Json.Num n ? n.value().longValueExact() : null;

        List<Json.Obj> tombs = new ArrayList<>();
        if (o.get("tombstones") instanceof Json.Arr arr) {
            for (Json.Value tv : arr) {
                if (!(tv instanceof Json.Obj)) {
                    throw new Json.JsonException("tombstones 元素必须是对象");
                }
                tombs.add((Json.Obj) tv);
            }
        }
        Json.Obj stats = o.get("stats") instanceof Json.Obj s ? s : new Json.Obj();
        return new SnapshotData(version, savedAt, wm, fh, seq, mode, genMax, tombs, stats);
    }

    // ---- 恢复为运行时对象 ----

    public List<Map.Entry<CompositeKey, Tombstone>> restoreEntries() {
        List<Map.Entry<CompositeKey, Tombstone>> out = new ArrayList<>();
        for (Json.Obj t : tombstones) {
            String key = t.getStr("key") == null ? "_default" : t.getStr("key");
            String id = Json.requireStr(t.get("id"), "id");
            out.add(Map.entry(new CompositeKey(key, id), Tombstone.fromJson(t)));
        }
        return out;
    }

    public Stats restoreStats() {
        Stats s = new Stats();
        s.seen = stats.get("seen") instanceof Json.Num n ? n.value().longValueExact() : 0;
        s.accepted = stats.get("accepted") instanceof Json.Num n ? n.value().longValueExact() : 0;
        s.suppressed = stats.get("suppressed") instanceof Json.Num n ? n.value().longValueExact() : 0;
        s.unguarded = stats.get("unguarded") instanceof Json.Num n ? n.value().longValueExact() : 0;
        s.payloadMismatch = stats.get("payloadMismatch") instanceof Json.Num n ? n.value().longValueExact() : 0;
        s.timeEvicted = stats.get("timeEvicted") instanceof Json.Num n ? n.value().longValueExact() : 0;
        s.forcedEvicted = stats.get("forcedEvicted") instanceof Json.Num n ? n.value().longValueExact() : 0;
        s.activeTombstonesPeak =
                stats.get("activeTombstonesPeak") instanceof Json.Num n ? n.value().longValueExact() : 0;
        return s;
    }

    /**
     * 把水位线生成器恢复到快照状态。模式不一致时以快照为准（bounded 的内部最大值也一并恢复）。
     */
    public void restoreGenerator(WatermarkGenerator generator) {
        if (generator instanceof ManualWatermarkGenerator m && watermark != null) {
            m.restore(watermark);
        } else if (generator instanceof BoundedOutOfOrdernessWatermarks b) {
            long maxTs = genMaxTimestamp == null
                    ? (watermark == null ? Long.MIN_VALUE : watermark)
                    : genMaxTimestamp;
            b.restore(maxTs, watermark);
        }
    }
}
