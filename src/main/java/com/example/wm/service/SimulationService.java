package com.example.wm.service;

import com.example.wm.engine.WatermarkCoordinator;
import com.example.wm.json.Json;
import com.example.wm.json.JsonException;
import com.example.wm.json.JsonWriter;
import com.example.wm.model.CoordinationSnapshot;
import com.example.wm.model.IngestionResult;
import com.example.wm.model.LateEvent;
import com.example.wm.model.PartitionStateView;
import com.example.wm.model.StreamEvent;
import com.example.wm.model.TickResult;
import com.example.wm.model.WatermarkConfig;
import com.example.wm.time.VirtualClock;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 确定性模拟服务：接收一段带显式处理时间的操作脚本，
 * 用 {@link VirtualClock} 单线程重放，返回每步结果与最终快照。
 * 不依赖任何真实调度，因此同样的请求永远得到同样的响应。
 */
public final class SimulationService {

    /** 执行一个已解析的 JSON 请求，返回可序列化的响应 Map。 */
    public Map<String, Object> execute(Map<String, Object> request) {
        long bound = reqLong(request, "boundMs", 0L);
        long idleTimeout = reqLong(request, "idleTimeoutMs", WatermarkConfig.NO_IDLE_TIMEOUT);
        long startTime = reqLong(request, "startTimeMs", 0L);
        boolean includeLate = reqBool(request, "includeLateEvents", true);

        WatermarkConfig config = new WatermarkConfig(bound, idleTimeout);
        VirtualClock clock = new VirtualClock(startTime);
        WatermarkCoordinator coordinator = new WatermarkCoordinator(config, clock);

        Object actionsObj = request.get("actions");
        if (actionsObj == null) {
            throw new JsonException("missing required field: actions");
        }
        if (!(actionsObj instanceof List<?>)) {
            throw new JsonException("actions must be an array");
        }

        List<Map<String, Object>> stepResults = new ArrayList<>();
        int index = 0;
        for (Object item : (List<?>) actionsObj) {
            if (!(item instanceof Map<?, ?>)) {
                throw new JsonException("action[" + index + "] must be an object");
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> action = (Map<String, Object>) item;
            stepResults.add(runAction(coordinator, clock, action, index));
            index++;
        }

        Map<String, Object> response = new LinkedHashMap<>();
        response.put("ok", true);
        response.put("processingTimeMs", clock.currentTimeMillis());
        response.put("snapshot", toMap(coordinator.snapshot()));
        response.put("globalWatermark", coordinator.globalWatermark());
        response.put("steps", stepResults);
        if (includeLate) {
            List<Object> lates = new ArrayList<>();
            for (LateEvent le : coordinator.lateEvents()) {
                lates.add(toMap(le));
            }
            response.put("lateEvents", lates);
        }
        return response;
    }

    /** 便捷入口：JSON 字符串 → JSON 字符串。 */
    public String executeJson(String requestJson) {
        return JsonWriter.pretty(execute(Json.parseObject(requestJson)));
    }

    // ------------------------------------------------------------------

    private Map<String, Object> runAction(WatermarkCoordinator c, VirtualClock clock,
                                          Map<String, Object> action, int index) {
        String type = str(action, "type", "action[" + index + "]");
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("index", index);
        out.put("type", type);

        switch (type) {
            case "register" -> {
                c.registerPartition(requiredPartition(action, index));
                out.put("processingTimeMs", clock.currentTimeMillis());
            }
            case "advance" -> {
                long duration = reqLong(action, "durationMs", 0L);
                clock.advanceBy(duration);
                out.put("processingTimeMs", clock.currentTimeMillis());
                out.put("advancedByMs", duration);
            }
            case "advanceTo" -> {
                long target = reqLong(action, "time", clock.currentTimeMillis());
                clock.advanceTo(target);
                out.put("processingTimeMs", clock.currentTimeMillis());
            }
            case "event" -> {
                Long time = optionalLong(action, "time");
                if (time != null) {
                    clock.advanceTo(time);
                }
                String partition = requiredPartition(action, index);
                Long eventTimeOpt = optionalLong(action, "eventTime");
                if (eventTimeOpt == null) {
                    throw new JsonException("event[" + index + "] requires eventTime");
                }
                long eventTime = eventTimeOpt;
                String payload = action.get("payload") == null
                        ? null : String.valueOf(action.get("payload"));
                StreamEvent event = new StreamEvent(partition, eventTime, payload);
                IngestionResult r = c.ingest(event);
                out.put("processingTimeMs", clock.currentTimeMillis());
                out.put("accepted", r.accepted());
                out.put("classification", r.classification().name());
                out.put("reason", r.reason() == null ? null : r.reason().name());
                out.put("resumedFromIdle", r.resumedFromIdle());
                out.put("previousGlobalWatermark", r.previousGlobalWatermark());
                out.put("globalWatermark", r.globalWatermark());
                out.put("timedOutPartitions", r.timedOutPartitions());
            }
            case "tick" -> {
                Long time = optionalLong(action, "time");
                if (time != null) {
                    clock.advanceTo(time);
                }
                TickResult r = c.tick();
                out.put("processingTimeMs", r.processingTimeMs());
                out.put("timedOutPartitions", r.timedOutPartitions());
                out.put("previousGlobalWatermark", r.previousGlobalWatermark());
                out.put("globalWatermark", r.globalWatermark());
            }
            case "pause" -> {
                Long time = optionalLong(action, "time");
                if (time != null) {
                    clock.advanceTo(time);
                }
                c.pausePartition(requiredPartition(action, index));
                out.put("processingTimeMs", clock.currentTimeMillis());
                out.put("globalWatermark", c.globalWatermark());
            }
            case "resume" -> {
                Long time = optionalLong(action, "time");
                if (time != null) {
                    clock.advanceTo(time);
                }
                Long eff = c.resumePartition(requiredPartition(action, index));
                out.put("processingTimeMs", clock.currentTimeMillis());
                out.put("effectiveWatermark", eff);
                out.put("globalWatermark", c.globalWatermark());
            }
            default -> throw new JsonException("unknown action type: " + type + " at index " + index);
        }
        return out;
    }

    // ------------------------------------------------------------------
    // 序列化
    // ------------------------------------------------------------------

    static Map<String, Object> toMap(CoordinationSnapshot s) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("processingTimeMs", s.processingTimeMs());
        m.put("globalWatermark", s.globalWatermark());
        m.put("activeCount", s.activeCount());
        m.put("idleCount", s.idleCount());
        m.put("pausedCount", s.pausedCount());
        List<Object> parts = new ArrayList<>();
        for (PartitionStateView p : s.partitions()) {
            parts.add(toMap(p));
        }
        m.put("partitions", parts);
        m.put("lateEventCount", s.lateEventCount());
        return m;
    }

    static Map<String, Object> toMap(PartitionStateView p) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("partition", p.partition());
        m.put("status", p.status().name());
        m.put("lastEventTimeMs", p.lastEventTimeMs());
        m.put("maxEventTimeMs", p.maxEventTimeMs());
        m.put("localWatermarkMs", p.localWatermarkMs());
        m.put("effectiveWatermarkMs", p.effectiveWatermarkMs());
        m.put("seenEvents", p.seenEvents());
        m.put("lateEvents", p.lateEvents());
        return m;
    }

    static Map<String, Object> toMap(LateEvent le) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("partition", le.event().partition());
        m.put("eventTimeMs", le.event().eventTimeMs());
        m.put("payload", le.event().payload());
        m.put("reason", le.reason().name());
        m.put("globalWatermarkMs", le.globalWatermarkMs());
        m.put("detectedAtProcessingMs", le.detectedAtProcessingMs());
        return m;
    }

    // ------------------------------------------------------------------
    // 小工具
    // ------------------------------------------------------------------

    private static String requiredPartition(Map<String, Object> action, int index) {
        Object v = action.get("partition");
        if (!(v instanceof String s) || s.isEmpty()) {
            throw new JsonException("action[" + index + "] requires non-empty string 'partition'");
        }
        return s;
    }

    private static String str(Map<String, Object> m, String key, String what) {
        Object v = m.get(key);
        if (!(v instanceof String s) || s.isEmpty()) {
            throw new JsonException(what + " requires non-empty string '" + key + "'");
        }
        return s;
    }

    private static long reqLong(Map<String, Object> m, String key, long dflt) {
        Object v = m.get(key);
        if (v == null) {
            return dflt;
        }
        return asLong(v, key);
    }

    private static long reqLong(Map<String, Object> m, String key, String error) {
        Object v = m.get(key);
        if (v == null) {
            throw new JsonException(error);
        }
        return asLong(v, key);
    }

    private static Long optionalLong(Map<String, Object> m, String key) {
        Object v = m.get(key);
        return v == null ? null : asLong(v, key);
    }

    private static boolean reqBool(Map<String, Object> m, String key, boolean dflt) {
        Object v = m.get(key);
        return v == null ? dflt : (Boolean) v;
    }

    private static long asLong(Object v, String key) {
        if (v instanceof Number n) {
            return n.longValue();
        }
        throw new JsonException("field '" + key + "' must be a number, got " + v.getClass().getSimpleName());
    }
}
