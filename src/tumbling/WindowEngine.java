package tumbling;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * 事件时间滚动窗口计数引擎（纯函数式状态机，不接触系统时钟）。
 *
 * 语义：
 *  - 窗口左闭右开 [k*size, (k+1)*size)，按 floor(eventTime/size) 归属（支持负时间戳）。
 *  - 每个分区维护显式水位线；窗口的触发/修订/最终关闭【只由本分区自己的水位】驱动，
 *    不被其他快/慢分区影响（对齐 Flink KeyedStream 上按键分区的窗口行为）。
 *  - 全局水位 = 活跃且已初始化分区水位的最小值，单调不减；它决定跨分区的
 *    “晚到 / 超容忍期侧输出”判定：事件时间戳 < 全局水位即晚到，
 *    且本分区窗口已过容忍期则进入侧输出。
 *  - 窗口触发：分区水位 >= windowEnd 时输出 fire；
 *    分区水位 >= windowEnd + allowedLateness 时输出 final 并清除状态。
 *    fire 与 final 同一步满足（容忍期 0 或水位跳变）时只输出一条 final。
 *  - 已 fire 的窗口在容忍期内收到晚到事件：立即再发一版 update（revision 递增）。
 *  - 同分区同 eventId 为重复事件：不计数，即使窗口已 purge 也能识别，且不进侧输出。
 *  - 分区可显式标记空闲：空闲分区不参与全局 min；发送事件/水位自动恢复。
 *  - 从未上报过水位的分区不参与全局 min；其事件不做迟到判定（新分区首批事件不受
 *    其他分区高水位误伤）。
 */
public final class WindowEngine {

    /** 进行中（未最终关闭）的窗口。 */
    private static final class Window {
        long start;
        long end;
        long count;
        long revision = 0;       // 每次内容修订 +1；首次触发时置 1
        boolean fired;
        long firedAt = Long.MIN_VALUE;
        long createdAt = Long.MIN_VALUE;
        long updatedAt = Long.MIN_VALUE;
    }

    /** 已最终关闭窗口的不可变快照。 */
    private static final class ClosedWindow {
        final long start, end, count, revision, firedAt, closedAt;
        ClosedWindow(Window w, long closedAt) {
            this.start = w.start;
            this.end = w.end;
            this.count = w.count;
            this.revision = w.revision;
            this.firedAt = w.firedAt;
            this.closedAt = closedAt;
        }
    }

    private static final class Partition {
        long watermark = Long.MIN_VALUE;
        boolean active = true;
        /** 是否已显式上报过水位。未初始化的分区不参与全局水位最小值。 */
        boolean initialized = false;
        final TreeMap<Long, Window> windows = new TreeMap<>();
        final java.util.Set<String> seen = new java.util.HashSet<>();
        final Map<Long, ClosedWindow> closed = new LinkedHashMap<>();
    }

    private long windowSize;
    private long allowedLateness;
    private long globalWatermark = Long.MIN_VALUE;
    private final Map<String, Partition> partitions = new LinkedHashMap<>();
    private final List<Map<String, Object>> sideOutput = new ArrayList<>();
    private final List<Map<String, Object>> duplicates = new ArrayList<>();
    private final List<Map<String, Object>> emissionLog = new ArrayList<>();

    public WindowEngine(long windowSize, long allowedLateness) {
        reconfigure(windowSize, allowedLateness);
    }

    public synchronized void reconfigure(long windowSize, long allowedLateness) {
        if (windowSize <= 0) throw new IllegalArgumentException("windowSize 必须为正数");
        if (allowedLateness < 0) throw new IllegalArgumentException("allowedLateness 不能为负数");
        this.windowSize = windowSize;
        this.allowedLateness = allowedLateness;
    }

    /** 清空全部状态（参数可选择覆盖）。 */
    public synchronized void reset(Long newWindowSize, Long newAllowedLateness) {
        partitions.clear();
        sideOutput.clear();
        duplicates.clear();
        emissionLog.clear();
        globalWatermark = Long.MIN_VALUE;
        if (newWindowSize != null || newAllowedLateness != null) {
            reconfigure(newWindowSize != null ? newWindowSize : windowSize,
                    newAllowedLateness != null ? newAllowedLateness : allowedLateness);
        }
    }

    private Partition partition(String id) {
        return partitions.computeIfAbsent(id, k -> new Partition());
    }

    /** floorDiv 语义的窗口起点：窗口左闭右开，支持负时间戳。 */
    private long windowStart(long eventTime) {
        long k = Math.floorDiv(eventTime, windowSize);
        try {
            return Math.multiplyExact(k, windowSize);
        } catch (ArithmeticException e) {
            throw new IllegalArgumentException("时间戳超出窗口可表示范围: " + eventTime);
        }
    }

    /** 窗口末端，溢出（时间戳接近 long 边界）时以 400 语义拒绝。 */
    private long windowEnd(long wStart) {
        try {
            return Math.addExact(wStart, windowSize);
        } catch (ArithmeticException e) {
            throw new IllegalArgumentException("窗口末端超出 long 可表示范围，起点: " + wStart);
        }
    }

    /** 容忍期截止时刻 end + allowedLateness。 */
    private long latenessDeadline(long wEnd) {
        try {
            return Math.addExact(wEnd, allowedLateness);
        } catch (ArithmeticException e) {
            throw new IllegalArgumentException("迟到容忍期截止时刻超出 long 可表示范围");
        }
    }

    /** 浅拷贝一条扁平记录（值均为不可变类型），切断内部存储与调用方之间的别名。 */
    private static Map<String, Object> copyOf(Map<String, Object> m) {
        return new LinkedHashMap<>(m);
    }

    /** 重算全局水位（活跃且已初始化分区的最小值，单调不减）。 */
    private long recomputeGlobalWatermark() {
        long min = Long.MAX_VALUE;
        for (Partition q : partitions.values()) {
            if (q.active && q.initialized) min = Math.min(min, q.watermark);
        }
        long candidate = (min == Long.MAX_VALUE) ? globalWatermark : min;
        globalWatermark = Math.max(globalWatermark, candidate);
        return globalWatermark;
    }

    // ============================ 事件接入 ============================

    /**
     * 接入一条事件。返回处理结论（status / 窗口 / 本次产生的输出）。
     */
    public synchronized Map<String, Object> ingest(String partitionId, String eventId, long eventTime) {
        if (eventId == null || eventId.isEmpty()) throw new IllegalArgumentException("eventId 不能为空");
        Partition p = partition(partitionId);
        boolean wasIdle = !p.active;
        p.active = true; // 事件到达自动恢复空闲分区

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("partition", partitionId);
        result.put("eventId", eventId);
        result.put("eventTime", eventTime);
        result.put("wasIdle", wasIdle);
        result.put("globalWatermarkBefore", globalWatermark);

        long wStart = windowStart(eventTime);
        long wEnd = windowEnd(wStart);
        result.put("windowStart", wStart);
        result.put("windowEnd", wEnd);

        // 1) 去重优先：即使窗口已 purge，重复事件仍然可识别且不计数。
        if (!p.seen.add(eventId)) {
            result.put("status", "duplicate");
            result.put("globalWatermarkAfter", globalWatermark);
            duplicates.add(copyOf(recordBase(partitionId, eventId, eventTime, wStart, globalWatermark, p.watermark)));
            result.put("emitted", List.of());
            return result;
        }

        // 2) 窗口是否还在容忍期由【本分区】水位决定：一旦本分区水位已越过
        //    windowEnd + allowedLateness（窗口已 final 并 purge），无论该事件相对全局
        //    水位是否“晚到”，都不能重建窗口，否则会再次 final 并覆盖关闭历史。
        boolean purged = p.initialized
                && (p.closed.containsKey(wStart) || latenessDeadline(wEnd) <= p.watermark);
        if (purged) {
            String reason = p.closed.containsKey(wStart) ? "window_finalized" : "after_allowed_lateness";
            result.put("status", "late_dropped");
            result.put("globalWatermarkAfter", globalWatermark);
            Map<String, Object> rec = recordBase(partitionId, eventId, eventTime, wStart,
                    globalWatermark, p.watermark);
            rec.put("reason", reason);
            sideOutput.add(copyOf(rec));
            result.put("emitted", List.of());
            return result;
        }

        // 3) 进入窗口（含：窗口已 fire 但容忍期内的晚到修订）。
        Window w = p.windows.get(wStart);
        if (w == null) {
            w = new Window();
            w.start = wStart;
            w.end = wEnd;
            w.createdAt = globalWatermark;
            p.windows.put(wStart, w);
        }
        w.count++;
        w.updatedAt = globalWatermark;

        boolean late = p.initialized && eventTime < globalWatermark;
        if (w.fired) {
            // 已触发窗口的修订：立即再发一版 update（水位戳记为本分区当前水位）。
            w.revision++;
            result.put("status", "late_accepted");
            Map<String, Object> upd = copyOf(emission("update", partitionId, w, p.watermark));
            emissionLog.add(upd);
            result.put("emitted", List.of(copyOf(upd)));
        } else {
            result.put("status", late ? "late_accepted" : "on_time");
            result.put("emitted", List.of());
        }
        result.put("count", w.count);
        result.put("revision", w.revision);
        result.put("globalWatermarkAfter", globalWatermark);
        return result;
    }

    // ============================ 批量（原子） ============================

    /** 一条待接入事件（已完成字段解析）。 */
    public record EventInput(String partition, String eventId, long eventTime) {}

    /** 一条待推进水位（已完成字段解析）。 */
    public record WatermarkInput(String partition, long watermark) {}

    /**
     * 原子批量接入：全部条目在同一把锁内顺序执行。调用方应先逐条解析（本方法会再次校验），
     * 任一条目非法则整体抛 IllegalArgumentException，且不产生任何状态变更。
     */
    public synchronized List<Map<String, Object>> ingestBatch(List<EventInput> events) {
        // 先校验全部，再执行任何修改（快速失败，保证批量原子语义）。
        for (EventInput ev : events) {
            if (ev.partition() == null || ev.partition().isEmpty()) {
                throw new IllegalArgumentException("partition 不能为空");
            }
            if (ev.eventId() == null || ev.eventId().isEmpty()) {
                throw new IllegalArgumentException("eventId 不能为空");
            }
            windowEnd(windowStart(ev.eventTime())); // 校验时间戳/窗口可表示范围
        }
        List<Map<String, Object>> results = new ArrayList<>(events.size());
        for (EventInput ev : events) {
            results.add(ingest(ev.partition(), ev.eventId(), ev.eventTime()));
        }
        return results;
    }

    /** 原子批量推进水位：先校验，再在同一把锁内顺序执行。 */
    public synchronized List<Map<String, Object>> advanceWatermarks(List<WatermarkInput> wms) {
        for (WatermarkInput w : wms) {
            if (w.partition() == null || w.partition().isEmpty()) {
                throw new IllegalArgumentException("partition 不能为空");
            }
        }
        List<Map<String, Object>> results = new ArrayList<>(wms.size());
        for (WatermarkInput w : wms) {
            results.add(advanceWatermark(w.partition(), w.watermark()));
        }
        return results;
    }

    // ============================ 水位推进 ============================

    /**
     * 设置某分区的显式水位线（单调；更低的值会被钳制），用本分区水位推进其窗口，
     * 再重算全局水位。
     */
    public synchronized Map<String, Object> advanceWatermark(String partitionId, long requested) {
        Partition p = partition(partitionId);
        boolean wasIdle = !p.active;
        p.active = true; // 显式水位也意味着分区恢复活跃

        long applied = Math.max(requested, p.watermark);
        boolean clamped = applied != requested;
        p.watermark = applied;
        p.initialized = true; // 首次显式水位之后，该分区才纳入全局水位计算

        // 窗口推进只看本分区自己的水位：其他分区快慢不影响本分区窗口的关闭时间。
        List<Map<String, Object>> emitted = advancePartitionWindows(p, partitionId, applied);

        long before = globalWatermark;
        long newGlobal = recomputeGlobalWatermark();

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("partition", partitionId);
        result.put("requestedWatermark", requested);
        result.put("appliedWatermark", applied);
        result.put("clamped", clamped);
        result.put("wasIdle", wasIdle);
        result.put("globalWatermarkBefore", before);
        result.put("globalWatermarkAfter", newGlobal);
        result.put("advanced", newGlobal > before);
        result.put("emitted", emitted);
        return result;
    }

    /** 用指定分区水位处理其窗口的触发与最终关闭。 */
    private List<Map<String, Object>> advancePartitionWindows(Partition p, String partitionId, long wm) {
        List<Map<String, Object>> emitted = new ArrayList<>();
        List<Long> toPurge = new ArrayList<>();
        for (Window w : p.windows.values()) {
            boolean willFire = !w.fired && w.end <= wm;
            boolean willClose = latenessDeadline(w.end) <= wm;
            if (willClose) {
                // 同一步既 fire 又 close（容忍期为 0 或水位跳变）：只发一条 final。
                w.fired = true;
                if (w.revision == 0) {
                    w.firedAt = wm;
                    w.revision = 1;
                }
                Map<String, Object> out = copyOf(emission("final", partitionId, w, wm));
                emissionLog.add(out);
                emitted.add(copyOf(out));
                p.closed.put(w.start, new ClosedWindow(w, wm));
                toPurge.add(w.start);
            } else if (willFire) {
                w.fired = true;
                w.firedAt = wm;
                w.revision = 1;
                Map<String, Object> out = copyOf(emission("fire", partitionId, w, wm));
                emissionLog.add(out);
                emitted.add(copyOf(out));
            }
        }
        for (Long k : toPurge) p.windows.remove(k);
        return emitted;
    }

    private Map<String, Object> emission(String type, String partitionId, Window w, long wm) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("type", type);
        m.put("partition", partitionId);
        m.put("windowStart", w.start);
        m.put("windowEnd", w.end);
        m.put("count", w.count);
        m.put("revision", Math.max(1, w.revision));
        m.put("watermark", wm);
        return m;
    }

    // ============================ 空闲分区 ============================

    public synchronized Map<String, Object> setIdle(String partitionId, boolean idle) {
        Partition p = partition(partitionId);
        boolean wasIdle = !p.active;
        p.active = !idle;
        long before = globalWatermark;
        long newGlobal = recomputeGlobalWatermark();

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("partition", partitionId);
        result.put("idle", !p.active);
        result.put("wasIdle", wasIdle);
        result.put("globalWatermarkBefore", before);
        result.put("globalWatermarkAfter", newGlobal);
        // 窗口由各分区自身水位驱动，空闲状态改变不产生窗口输出。
        result.put("emitted", List.of());
        return result;
    }

    // ============================ 查询视图 ============================

    public synchronized Map<String, Object> snapshot() {
        Map<String, Object> snap = new LinkedHashMap<>();
        snap.put("config", Map.of("windowSize", windowSize, "allowedLateness", allowedLateness));
        snap.put("globalWatermark", globalWatermark);

        List<Object> parts = new ArrayList<>();
        for (Map.Entry<String, Partition> e : partitions.entrySet()) {
            Partition p = e.getValue();
            Map<String, Object> pm = new LinkedHashMap<>();
            pm.put("partition", e.getKey());
            pm.put("watermark", p.watermark);
            pm.put("initialized", p.initialized);
            pm.put("idle", !p.active);
            List<Object> ws = new ArrayList<>();
            for (Window w : p.windows.values()) {
                Map<String, Object> wm = new LinkedHashMap<>();
                wm.put("windowStart", w.start);
                wm.put("windowEnd", w.end);
                wm.put("count", w.count);
                wm.put("revision", w.revision);
                wm.put("fired", w.fired);
                wm.put("firedAt", w.fired ? w.firedAt : null);
                wm.put("createdAt", w.createdAt);
                wm.put("updatedAt", w.updatedAt);
                ws.add(wm);
            }
            pm.put("openWindows", ws);
            List<Object> cw = new ArrayList<>();
            for (ClosedWindow c : p.closed.values()) {
                Map<String, Object> cm = new LinkedHashMap<>();
                cm.put("windowStart", c.start);
                cm.put("windowEnd", c.end);
                cm.put("count", c.count);
                cm.put("revision", c.revision);
                cm.put("firedAt", c.firedAt);
                cm.put("closedAt", c.closedAt);
                cw.add(cm);
            }
            pm.put("closedWindows", cw);
            parts.add(pm);
        }
        snap.put("partitions", parts);
        snap.put("sideOutput", copyRecords(sideOutput));
        snap.put("duplicates", copyRecords(duplicates));
        snap.put("emissionLog", copyRecords(emissionLog));
        return snap;
    }

    public synchronized List<Map<String, Object>> windowsView(String partitionId) {
        List<Map<String, Object>> out = new ArrayList<>();
        Partition pp = partitions.get(partitionId);
        if (pp == null) return out;
        for (ClosedWindow c : pp.closed.values()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("partition", partitionId);
            m.put("windowStart", c.start);
            m.put("windowEnd", c.end);
            m.put("count", c.count);
            m.put("revision", c.revision);
            m.put("firedAt", c.firedAt);
            m.put("closedAt", c.closedAt);
            m.put("state", "final");
            out.add(m);
        }
        for (Window w : pp.windows.values()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("partition", partitionId);
            m.put("windowStart", w.start);
            m.put("windowEnd", w.end);
            m.put("count", w.count);
            m.put("revision", w.revision);
            m.put("state", w.fired ? "fired" : "open");
            if (w.fired) m.put("firedAt", w.firedAt);
            out.add(m);
        }
        return out;
    }

    public synchronized List<Map<String, Object>> sideOutputView() {
        return copyRecords(sideOutput);
    }

    public synchronized List<Map<String, Object>> duplicatesView() {
        return copyRecords(duplicates);
    }

    public synchronized List<Map<String, Object>> emissionsView() {
        return copyRecords(emissionLog);
    }

    public synchronized Map<String, Object> configView() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("windowSize", windowSize);
        m.put("allowedLateness", allowedLateness);
        m.put("globalWatermark", globalWatermark);
        m.put("partitionCount", partitions.size());
        return m;
    }

    private static Map<String, Object> recordBase(String partitionId, String eventId, long eventTime,
                                                  long windowStart, long globalWm, long partitionWm) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("partition", partitionId);
        m.put("eventId", eventId);
        m.put("eventTime", eventTime);
        m.put("windowStart", windowStart);
        m.put("atGlobalWatermark", globalWm);
        m.put("atPartitionWatermark", partitionWm);
        return m;
    }

    /** 浅拷贝一份记录列表（值均为不可变类型）。 */
    private static List<Map<String, Object>> copyRecords(List<Map<String, Object>> src) {
        List<Map<String, Object>> dst = new ArrayList<>(src.size());
        for (Map<String, Object> m : src) dst.add(copyOf(m));
        return dst;
    }

    // ---- 供测试使用的基本访问器 ----
    public synchronized long getGlobalWatermark() { return globalWatermark; }
    public synchronized long getWindowSize() { return windowSize; }
    public synchronized long getAllowedLateness() { return allowedLateness; }
}
