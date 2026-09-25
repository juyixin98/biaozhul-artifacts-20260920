package streammatch.service;

import streammatch.engine.StreamMatcher;
import streammatch.model.EngineConfig;
import streammatch.model.EngineMode;
import streammatch.model.EngineResult;
import streammatch.model.Event;
import streammatch.model.LatePolicy;
import streammatch.model.Match;
import streammatch.model.MatchPolicy;
import streammatch.model.RemovedA;
import streammatch.reference.NaiveReferenceMatcher;
import streammatch.time.Clock;
import streammatch.time.SystemClock;
import streammatch.time.TaskScheduler;
import streammatch.time.WallScheduler;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * 有状态匹配服务：持有一个常驻流式引擎，并提供校验、配置、重放对照等操作。
 * 所有公开方法在 {@code this} 上同步——服务的整个“状态视图”原子切换，HTTP 层无需额外加锁。
 */
public final class MatchingService implements AutoCloseable {

    private final Clock clock;
    private final TaskScheduler scheduler;
    private final WallScheduler wallScheduler; // 仅自建时非 null，随服务关闭

    private EngineConfig config;
    private StreamMatcher engine;
    private final List<Event> acceptedEvents = new ArrayList<>();

    public MatchingService(EngineConfig initial) {
        this(initial, null, null, null);
    }

    /** 测试用：注入时钟与调度器（仅处理时间模式有意义）。 */
    public MatchingService(EngineConfig initial, Clock clock, TaskScheduler scheduler) {
        this(validateConfigShape(initial), clock, scheduler, null);
    }

    private MatchingService(EngineConfig cfg, Clock clock, TaskScheduler scheduler,
                            WallScheduler ownedWallScheduler) {
        this.config = cfg;
        this.clock = clock;
        this.scheduler = scheduler;
        this.wallScheduler = ownedWallScheduler;
        this.engine = buildEngine(cfg);
    }

    /** 服务主程序使用：按模式自建墙钟资源。 */
    public static MatchingService createWithWallClock(EngineConfig initial) {
        EngineConfig cfg = validateConfigShape(initial);
        if (cfg.mode() == EngineMode.PROCESSING_TIME) {
            WallScheduler s = new WallScheduler(SystemClock.INSTANCE, "streammatch-timeout");
            return new MatchingService(cfg, SystemClock.INSTANCE, s, s);
        }
        return new MatchingService(cfg, null, null, null);
    }

    private StreamMatcher buildEngine(EngineConfig cfg) {
        if (cfg.mode() == EngineMode.EVENT_TIME) {
            return StreamMatcher.eventTime(cfg);
        }
        if (clock == null || scheduler == null) {
            throw new ApiException(400, "BAD_REQUEST",
                    "PROCESSING_TIME mode requires injected clock/scheduler");
        }
        return StreamMatcher.processingTime(cfg, clock, scheduler);
    }

    // ---------------------------------------------------------------- 配置

    public synchronized Map<String, Object> configView() {
        return configToMap(config);
    }

    public synchronized Map<String, Object> reconfigure(Map<String, Object> req) {
        EngineConfig next = parseConfig(req, config.mode());
        if (next.mode() != config.mode()) {
            throw ApiException.unprocessable("MODE_IMMUTABLE",
                    "mode cannot change at runtime; current=" + config.mode());
        }
        engine.reconfigure(next);
        this.config = next;
        return configToMap(next);
    }

    static EngineConfig validateConfigShape(EngineConfig cfg) {
        // record 构造器已做基本校验，这里集中给出可读错误
        if (cfg == null) {
            throw ApiException.badRequest("config is required");
        }
        return cfg;
    }

    @SuppressWarnings("unchecked")
    static EngineConfig parseConfig(Map<String, Object> req, EngineMode defaultMode) {
        if (req == null) {
            throw ApiException.badRequest("request body must be a JSON object");
        }
        EngineMode mode = defaultMode;
        Object modeRaw = req.get("mode");
        if (modeRaw != null) {
            try {
                mode = EngineMode.valueOf(String.valueOf(modeRaw));
            } catch (IllegalArgumentException ex) {
                throw ApiException.badRequest("mode must be EVENT_TIME or PROCESSING_TIME: " + modeRaw);
            }
        }
        long window = requirePositiveLong(req, "windowMillis", 1000L);
        long allowedLateness = requireNonNegativeLong(req, "allowedLatenessMillis", 0L);

        MatchPolicy policy = MatchPolicy.ALL_CANDIDATES;
        Object p = req.get("matchPolicy");
        if (p != null) {
            try {
                policy = MatchPolicy.valueOf(String.valueOf(p));
            } catch (IllegalArgumentException ex) {
                throw ApiException.badRequest(
                        "matchPolicy must be ALL_CANDIDATES or SKIP_PAST_LAST: " + p);
            }
        }
        LatePolicy latePolicy = LatePolicy.DROP;
        Object lp = req.get("latePolicy");
        if (lp != null) {
            try {
                latePolicy = LatePolicy.valueOf(String.valueOf(lp));
            } catch (IllegalArgumentException ex) {
                throw ApiException.badRequest("latePolicy must be DROP or REJECT: " + lp);
            }
        }
        try {
            return new EngineConfig(mode, window, policy, allowedLateness, latePolicy);
        } catch (IllegalArgumentException ex) {
            throw ApiException.badRequest(ex.getMessage());
        }
    }

    static Map<String, Object> configToMap(EngineConfig cfg) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("mode", cfg.mode().name());
        m.put("windowMillis", cfg.windowMillis());
        m.put("matchPolicy", cfg.matchPolicy().name());
        m.put("allowedLatenessMillis", cfg.allowedLateness());
        m.put("latePolicy", cfg.latePolicy().name());
        return m;
    }

    // ---------------------------------------------------------------- 事件

    /**
     * 校验并提交一批事件。事件 {@code seq} 由引擎分配；请求只需 id/key/type/timestamp。
     */
    public synchronized Map<String, Object> ingest(Map<String, Object> req) {
        List<Event> events = parseEvents(req);
        EngineResult result;
        try {
            result = engine.process(events);
        } catch (StreamMatcher.LateEventException ex) {
            throw ApiException.unprocessable("LATE_EVENTS_REJECTED",
                    "late events under REJECT policy: " + ex.lateIds());
        }
        // 仅记录“被接受”的事件（DROP 的迟到事件不进入正式状态，也不进入重放集）
        Set<String> dropped = new HashSet<>(result.lateDropped());
        for (Event e : events) {
            if (!dropped.contains(e.id())) {
                acceptedEvents.add(e);
            }
        }

        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("processed", events.size());
        resp.put("matches", matchesToMaps(result.matches()));
        resp.put("removed", removedToMaps(result.removed()));
        resp.put("lateDropped", result.lateDropped());
        resp.put("watermarkMillis", result.watermarkMillis() == Long.MIN_VALUE
                ? null : result.watermarkMillis());
        resp.put("activeA", result.activeAKeys());
        resp.put("totalMatches", engine.matches().size());
        return resp;
    }

    @SuppressWarnings("unchecked")
    private List<Event> parseEvents(Map<String, Object> req) {
        Object raw = req.get("events");
        if (!(raw instanceof List<?> list)) {
            throw ApiException.badRequest("field 'events' must be an array");
        }
        List<Event> events = new ArrayList<>(list.size());
        Set<String> batchIds = new HashSet<>();
        for (Object o : list) {
            if (!(o instanceof Map<?, ?> em)) {
                throw ApiException.badRequest("each event must be an object");
            }
            String id = requireString((Map<String, Object>) em, "id");
            String key = requireString((Map<String, Object>) em, "key");
            String type = requireString((Map<String, Object>) em, "type");
            long ts = asLong(em.get("timestamp"), "timestamp");
            if (!Event.A.equals(type) && !Event.B.equals(type) && !Event.C.equals(type)) {
                throw ApiException.badRequest("event type must be A, B or C (got '" + type + "')");
            }
            if (!batchIds.add(id)) {
                throw ApiException.badRequest("duplicate event id within request: " + id);
            }
            if (acceptedEvents.stream().anyMatch(e -> e.id().equals(id))) {
                throw ApiException.unprocessable("DUPLICATE_EVENT_ID",
                        "event id already accepted by the engine: " + id);
            }
            events.add(new Event(id, key, type, ts, 0L));
        }
        return events;
    }

    // ---------------------------------------------------------------- 查询

    public synchronized Map<String, Object> state() {
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("config", configToMap(config));
        resp.put("acceptedEventCount", acceptedEvents.size());
        resp.put("matches", matchesToMaps(engine.matches()));
        resp.put("activeA", engine.activeAIds());
        resp.put("watermarkMillis", engine.watermark() == Long.MIN_VALUE ? null : engine.watermark());
        return resp;
    }

    public synchronized Map<String, Object> advanceWatermark(Map<String, Object> req) {
        if (config.mode() != EngineMode.EVENT_TIME) {
            throw ApiException.unprocessable("UNSUPPORTED_MODE",
                    "watermark advancement is only valid in EVENT_TIME mode");
        }
        long target = asLong(req == null ? null : req.get("watermarkMillis"), "watermarkMillis");
        EngineResult r = engine.advanceWatermark(target);
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("removed", removedToMaps(r.removed()));
        resp.put("watermarkMillis", r.watermarkMillis());
        resp.put("activeA", r.activeAKeys());
        return resp;
    }

    public synchronized Map<String, Object> reset(Map<String, Object> req) {
        EngineConfig cfg = config;
        if (req != null && !req.isEmpty()) {
            cfg = parseConfig(req, config.mode());
            if (cfg.mode() != config.mode()) {
                throw ApiException.unprocessable("MODE_IMMUTABLE",
                        "mode cannot change at runtime; create a fresh service to switch mode");
            }
        }
        engine.reset();
        acceptedEvents.clear();
        engine.reconfigure(cfg);
        this.config = cfg;
        return state();
    }

    // ---------------------------------------------------------------- 重放

    /**
     * 对给定事件（默认=服务自启动以来接受的全部事件）做确定性重放：
     * 按 {@code (timestamp, 原始到达序)} 排序后喂给一个全新的同配置流式引擎，
     * 再与暴力参考实现逐对比较。
     */
    @SuppressWarnings("unchecked")
    public synchronized Map<String, Object> replay(Map<String, Object> req) {
        List<Event> source;
        if (req != null && req.get("events") instanceof List<?> list && !list.isEmpty()) {
            source = parseEventsLoose(list);
        } else {
            source = new ArrayList<>(acceptedEvents);
        }

        // 配置：允许 replay 自带 config（仅用于本次重放计算），否则用当前服务配置
        EngineConfig cfg = (req != null && req.get("config") instanceof Map<?, ?> cm)
                ? parseConfig((Map<String, Object>) cm, config.mode())
                : config;
        if (cfg.mode() != EngineMode.EVENT_TIME) {
            throw ApiException.unprocessable("UNSUPPORTED_MODE",
                    "replay is only defined for EVENT_TIME mode: processing-time semantics "
                            + "depend on wall-clock arrival and cannot be reconstructed from history");
        }

        // 排序后从 0 重新编号 seq：构造理想（无迟到）到达全序。
        // 注意 tie-break 必须用“服务接受顺序”（acceptedEvents 的下标 / 外部数组顺序），
        // 不能用引擎全局 seq——乱序批处理会让它与接受顺序不一致。
        List<Event> sourceForReplay = new ArrayList<>();
        for (int i = 0; i < source.size(); i++) {
            sourceForReplay.add(withSeq(source.get(i), i));
        }
        List<Event> ordered = new ArrayList<>(sourceForReplay);
        ordered.sort(Comparator.comparingLong(Event::timestamp).thenComparingLong(Event::seq));
        for (int i = 0; i < ordered.size(); i++) {
            ordered.set(i, withSeq(ordered.get(i), i));
        }

        // 1) 全新流式引擎按理想顺序重放。
        // 喂给引擎的 Event.seq 会被引擎忽略（引擎按喂入顺序自行编号），所以这里只需保证
        // 喂入顺序与下面参考实现看到的全序一致即可——两边使用同一个 ordered 列表。
        StreamMatcher replayEngine = StreamMatcher.eventTime(cfg);
        for (Event e : ordered) {
            replayEngine.process(List.of(e));
        }
        List<Match> streamMatches = replayEngine.matches();

        // 2) 暴力参考实现
        NaiveReferenceMatcher.ReferenceResult ref =
                NaiveReferenceMatcher.compute(ordered, cfg.windowMillis(), cfg.matchPolicy());

        List<Map<String, Object>> refMaps = new ArrayList<>();
        for (NaiveReferenceMatcher.RefMatch m : ref.matches()) {
            Map<String, Object> mm = new LinkedHashMap<>();
            mm.put("key", m.key());
            mm.put("aId", m.aId());
            mm.put("bId", m.bId());
            mm.put("aTimestamp", m.aTimestamp());
            mm.put("bTimestamp", m.bTimestamp());
            refMaps.add(mm);
        }

        // 3) 一致性判定（按“集合”比较：同 key/aId/bId 三元组）
        Set<String> streamPairs = new HashSet<>();
        for (Match m : streamMatches) {
            streamPairs.add(m.key() + "|" + m.aId() + "|" + m.bId());
        }
        Set<String> refPairs = new HashSet<>();
        for (NaiveReferenceMatcher.RefMatch m : ref.matches()) {
            refPairs.add(m.key() + "|" + m.aId() + "|" + m.bId());
        }
        boolean consistent = streamPairs.equals(refPairs);

        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("replayedEventCount", ordered.size());
        resp.put("config", configToMap(cfg));
        resp.put("streamReplayMatches", matchesToMaps(streamMatches));
        resp.put("referenceMatches", refMaps);
        resp.put("consistent", consistent);
        if (!consistent) {
            resp.put("onlyInStream", new ArrayList<>(minus(streamPairs, refPairs)));
            resp.put("onlyInReference", new ArrayList<>(minus(refPairs, streamPairs)));
        }
        return resp;
    }

    @SuppressWarnings("unchecked")
    private List<Event> parseEventsLoose(List<?> list) {
        List<Event> events = new ArrayList<>(list.size());
        Set<String> ids = new HashSet<>();
        long seq = 0;
        for (Object o : list) {
            if (!(o instanceof Map<?, ?> em)) {
                throw ApiException.badRequest("each event must be an object");
            }
            String id = requireString((Map<String, Object>) em, "id");
            String key = requireString((Map<String, Object>) em, "key");
            String type = requireString((Map<String, Object>) em, "type");
            long ts = asLong(em.get("timestamp"), "timestamp");
            if (!Event.A.equals(type) && !Event.B.equals(type) && !Event.C.equals(type)) {
                throw ApiException.badRequest("event type must be A, B or C");
            }
            if (!ids.add(id)) {
                throw ApiException.badRequest("duplicate event id in replay input: " + id);
            }
            events.add(new Event(id, key, type, ts, seq++));
        }
        return events;
    }

    private static Set<String> minus(Set<String> a, Set<String> b) {
        Set<String> r = new java.util.TreeSet<>(a);
        r.removeAll(b);
        return r;
    }

    private static Event withSeq(Event e, long seq) {
        return new Event(e.id(), e.key(), e.type(), e.timestamp(), seq);
    }

    // ---------------------------------------------------------------- 序列化辅助

    static List<Map<String, Object>> matchesToMaps(List<Match> matches) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (Match m : matches) {
            Map<String, Object> mm = new LinkedHashMap<>();
            mm.put("key", m.key());
            mm.put("aId", m.aId());
            mm.put("bId", m.bId());
            mm.put("aTimestamp", m.aTimestamp());
            mm.put("bTimestamp", m.bTimestamp());
            mm.put("aSeq", m.aSeq());
            mm.put("bSeq", m.bSeq());
            mm.put("emitIndex", m.emitIndex());
            out.add(mm);
        }
        return out;
    }

    static List<Map<String, Object>> removedToMaps(List<RemovedA> removed) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (RemovedA r : removed) {
            Map<String, Object> mm = new LinkedHashMap<>();
            mm.put("key", r.key());
            mm.put("aId", r.aId());
            mm.put("reason", r.reason().name());
            if (r.cId() != null) {
                mm.put("cId", r.cId());
            }
            out.add(mm);
        }
        return out;
    }

    private static String requireString(Map<String, Object> m, String field) {
        Object v = m.get(field);
        if (!(v instanceof String s) || s.isEmpty()) {
            throw ApiException.badRequest("field '" + field + "' must be a non-empty string");
        }
        return s;
    }

    private static long requirePositiveLong(Map<String, Object> m, String field, long dflt) {
        long v = m.containsKey(field) ? asLong(m.get(field), field) : dflt;
        if (v <= 0) {
            throw ApiException.badRequest("field '" + field + "' must be > 0");
        }
        return v;
    }

    private static long requireNonNegativeLong(Map<String, Object> m, String field, long dflt) {
        long v = m.containsKey(field) ? asLong(m.get(field), field) : dflt;
        if (v < 0) {
            throw ApiException.badRequest("field '" + field + "' must be >= 0");
        }
        return v;
    }

    private static long asLong(Object v, String field) {
        if (v instanceof Number n) {
            return n.longValue();
        }
        if (v instanceof String s) {
            try {
                return Long.parseLong(s);
            } catch (NumberFormatException ex) {
                throw ApiException.badRequest("field '" + field + "' must be an integer");
            }
        }
        throw ApiException.badRequest("field '" + field + "' must be an integer");
    }

    @Override
    public synchronized void close() {
        if (wallScheduler != null) {
            wallScheduler.close();
        }
    }
}
