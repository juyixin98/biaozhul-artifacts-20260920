package com.tjoin.service;

import com.tjoin.core.BufferCapacityExceededException;
import com.tjoin.core.Collector;
import com.tjoin.core.Event;
import com.tjoin.core.JoinConfig;
import com.tjoin.core.JoinMetrics;
import com.tjoin.core.JoinPair;
import com.tjoin.core.IntervalJoinOperator;
import com.tjoin.core.StreamSide;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 由 JSON 请求驱动的区间连接模拟：无状态服务逻辑（每个请求新建算子），
 * 便于通过 HTTP 或直接调用进行端到端验证。
 *
 * <p>支持两种输入形态：
 * <ul>
 *   <li><b>steps</b>：显式给出事件与水位线的交错顺序，用于演示停滞、边界、提前清理等；</li>
 *   <li><b>simple</b>：给出 left/right 两个事件数组，按 (时间戳, 侧别, 原始下标) 确定性回放，
 *       两侧独立生成水位线，可选最终推进一个水位线以观察状态清理。</li>
 * </ul>
 */
public final class JoinSimulation {

    private JoinSimulation() {
    }

    public static Map<String, Object> run(Map<String, Object> request) {
        JoinConfig config = parseConfig(request.get("config"));
        JoinMetrics metrics = new JoinMetrics();
        IntervalJoinOperator op = new IntervalJoinOperator(config, metrics);

        List<Map<String, Object>> results = new ArrayList<>();
        Collector collector = pair -> results.add(pairToMap(pair));

        List<Map<String, Object>> trace = new ArrayList<>();
        try {
            Object steps = request.get("steps");
            if (steps != null) {
                runSteps(op, collector, asList(steps, "steps"), trace);
            } else {
                runSimple(op, collector, request, trace);
            }
        } catch (BufferCapacityExceededException e) {
            throw new CapacityException(e);
        }

        Map<String, Object> response = new LinkedHashMap<>();
        response.put("ok", true);
        response.put("results", results);
        response.put("trace", trace);
        response.put("watermarks", Map.of(
                "left", op.watermark(StreamSide.LEFT),
                "right", op.watermark(StreamSide.RIGHT),
                "output", op.outputWatermark()));
        response.put("buffered", Map.of(
                "left", op.bufferedCount(StreamSide.LEFT),
                "right", op.bufferedCount(StreamSide.RIGHT)));
        response.put("metrics", metricsToMap(metrics));
        return response;
    }

    // ------------------------------------------------------------------
    // steps 模式
    // ------------------------------------------------------------------

    private static void runSteps(IntervalJoinOperator op, Collector collector,
                                 List<?> steps, List<Map<String, Object>> trace) {
        for (int i = 0; i < steps.size(); i++) {
            Map<?, ?> step = asMap(steps.get(i), "steps[" + i + "]");
            String type = requireString(step, "type", "steps[" + i + "]");
            switch (type) {
                case "event": {
                    Event e = parseEvent(step, "steps[" + i + "]");
                    List<JoinPair> out = op.processEvent(e, collector);
                    trace.add(stepTrace("event", e.side(), e.id(), e.timestamp(),
                            out.size(), op, null));
                    break;
                }
                case "watermark":
                case "wm": {
                    StreamSide side = parseSide(requireString(step, "side", "steps[" + i + "]"));
                    long wm = asLong(step.get("watermark") != null ? step.get("watermark") : step.get("wm"),
                            "steps[" + i + "].watermark");
                    long beforeL = op.bufferedCount(StreamSide.LEFT);
                    long beforeR = op.bufferedCount(StreamSide.RIGHT);
                    op.processWatermark(side, wm);
                    trace.add(stepTrace("watermark", side, null, wm, 0, op,
                            Map.of("expiredFromLeft", beforeL - op.bufferedCount(StreamSide.LEFT),
                                    "expiredFromRight", beforeR - op.bufferedCount(StreamSide.RIGHT))));
                    break;
                }
                default:
                    throw new BadRequestException("unknown step type: " + type);
            }
        }
    }

    // ------------------------------------------------------------------
    // simple 模式
    // ------------------------------------------------------------------

    private static void runSimple(IntervalJoinOperator op, Collector collector,
                                  Map<String, Object> request, List<Map<String, Object>> trace) {
        List<Indexed> events = new ArrayList<>();
        collectEvents(asList(request.getOrDefault("left", List.of()), "left"),
                StreamSide.LEFT, events);
        collectEvents(asList(request.getOrDefault("right", List.of()), "right"),
                StreamSide.RIGHT, events);

        long outOfOrderness = optLong(request.get("outOfOrderness"), 0L);
        long advanceTo = optLong(request.get("advanceWatermarkTo"), Long.MIN_VALUE);

        // 确定性回放顺序：事件时间戳 → 侧别(LEFT 先) → 原始数组下标
        events.sort(Comparator.comparingLong((Indexed ix) -> ix.event.timestamp())
                .thenComparing(ix -> ix.event.side())
                .thenComparingInt(ix -> ix.index));

        long leftMax = Long.MIN_VALUE;
        long rightMax = Long.MIN_VALUE;
        for (Indexed ix : events) {
            Event e = ix.event;
            // 按有界乱序策略先推进本侧水位线，两侧完全独立
            if (e.side() == StreamSide.LEFT) {
                leftMax = Math.max(leftMax, e.timestamp());
                long candidate = leftMax - outOfOrderness;
                if (candidate > op.watermark(StreamSide.LEFT)) {
                    op.processWatermark(StreamSide.LEFT, candidate);
                }
            } else {
                rightMax = Math.max(rightMax, e.timestamp());
                long candidate = rightMax - outOfOrderness;
                if (candidate > op.watermark(StreamSide.RIGHT)) {
                    op.processWatermark(StreamSide.RIGHT, candidate);
                }
            }
            List<JoinPair> out = op.processEvent(e, collector);
            trace.add(stepTrace("event", e.side(), e.id(), e.timestamp(), out.size(), op, null));
        }

        if (advanceTo != Long.MIN_VALUE) {
            String sideStr = String.valueOf(request.getOrDefault("advanceSide", "LEFT"));
            StreamSide side = parseSide(sideStr);
            long beforeL = op.bufferedCount(StreamSide.LEFT);
            long beforeR = op.bufferedCount(StreamSide.RIGHT);
            op.processWatermark(side, advanceTo);
            trace.add(stepTrace("watermark", side, null, advanceTo, 0, op,
                    Map.of("expiredFromLeft", beforeL - op.bufferedCount(StreamSide.LEFT),
                            "expiredFromRight", beforeR - op.bufferedCount(StreamSide.RIGHT))));
        }
    }

    /** 携带原始数组下标的事件（回放排序后仍可稳定决胜）。 */
    private record Indexed(Event event, int index) {
    }

    private static void collectEvents(List<?> raw, StreamSide side, List<Indexed> sink) {
        for (int i = 0; i < raw.size(); i++) {
            Map<?, ?> m = asMap(raw.get(i), side == StreamSide.LEFT ? "left" : "right");
            String id = requireString(m, "id", "event");
            String key = requireString(m, "key", "event");
            long ts = asLong(m.get("ts") != null ? m.get("ts") : m.get("timestamp"), "ts");
            Object value = m.get("value");
            sink.add(new Indexed(new Event(id, side, key, ts, value), i));
        }
    }

    // ------------------------------------------------------------------
    // 解析辅助
    // ------------------------------------------------------------------

    private static JoinConfig parseConfig(Object raw) {
        if (raw == null) {
            throw new BadRequestException("missing 'config' object");
        }
        Map<?, ?> m = asMap(raw, "config");
        JoinConfig.Builder b = JoinConfig.builder();
        Long lower = optLong(m.get("lowerBound"), null);
        Long upper = optLong(m.get("upperBound"), null);
        if (lower == null || upper == null) {
            throw new BadRequestException("config requires lowerBound and upperBound");
        }
        b.lowerBound(lower).upperBound(upper);
        b.lowerInclusive(optBool(m.get("lowerInclusive"), true));
        b.upperInclusive(optBool(m.get("upperInclusive"), true));
        int cap = (int) Math.min(Integer.MAX_VALUE, optLong(m.get("maxBufferedPerSide"), 0L));
        b.maxBufferedPerSide(cap);
        try {
            return b.build();
        } catch (IllegalArgumentException ex) {
            throw new BadRequestException(ex.getMessage());
        }
    }

    private static Event parseEvent(Map<?, ?> m, String where) {
        StreamSide side = parseSide(requireString(m, "side", where));
        String id = requireString(m, "id", where);
        String key = requireString(m, "key", where);
        long ts = asLong(m.get("ts") != null ? m.get("ts") : m.get("timestamp"), where + ".ts");
        return new Event(id, side, key, ts, m.get("value"));
    }

    private static StreamSide parseSide(String s) {
        if (s == null) {
            throw new BadRequestException("missing side (LEFT/RIGHT)");
        }
        try {
            return StreamSide.valueOf(s.trim().toUpperCase());
        } catch (IllegalArgumentException e) {
            throw new BadRequestException("invalid side: " + s);
        }
    }

    private static Map<String, Object> eventToMap(Event e) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", e.id());
        m.put("side", e.side().name());
        m.put("key", e.key());
        m.put("ts", e.timestamp());
        m.put("value", e.value());
        return m;
    }

    private static Map<String, Object> pairToMap(JoinPair p) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("key", p.key());
        m.put("left", eventToMap(p.left()));
        m.put("right", eventToMap(p.right()));
        m.put("tsDiff", p.right().timestamp() - p.left().timestamp());
        return m;
    }

    private static Map<String, Object> stepTrace(String type, StreamSide side, String id,
                                                 long ts, int emitted,
                                                 IntervalJoinOperator op,
                                                 Map<String, ?> extra) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("type", type);
        m.put("side", side.name());
        if (id != null) {
            m.put("id", id);
        }
        m.put("ts", ts);
        m.put("emitted", emitted);
        m.put("buffered", Map.of(
                "left", op.bufferedCount(StreamSide.LEFT),
                "right", op.bufferedCount(StreamSide.RIGHT)));
        m.put("watermarks", Map.of(
                "left", op.watermark(StreamSide.LEFT),
                "right", op.watermark(StreamSide.RIGHT)));
        if (extra != null) {
            m.putAll(extra);
        }
        return m;
    }

    private static Map<String, Object> metricsToMap(JoinMetrics x) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("leftReceived", x.leftReceived());
        m.put("rightReceived", x.rightReceived());
        m.put("duplicates", x.duplicates());
        m.put("lateDropped", x.lateDropped());
        m.put("emitted", x.emitted());
        m.put("leftExpired", x.leftExpired());
        m.put("rightExpired", x.rightExpired());
        m.put("staleWatermarks", x.staleWatermarks());
        return m;
    }

    // ---- 小工具 ----

    private static Map<?, ?> asMap(Object o, String where) {
        if (!(o instanceof Map)) {
            throw new BadRequestException(where + " must be an object");
        }
        return (Map<?, ?>) o;
    }

    private static List<?> asList(Object o, String where) {
        if (!(o instanceof List)) {
            throw new BadRequestException(where + " must be an array");
        }
        return (List<?>) o;
    }

    private static String requireString(Map<?, ?> m, String field, String where) {
        Object v = m.get(field);
        if (!(v instanceof String) || ((String) v).isEmpty()) {
            throw new BadRequestException(where + " requires non-empty string field '" + field + "'");
        }
        return (String) v;
    }

    private static long asLong(Object v, String where) {
        if (v instanceof Number) {
            return ((Number) v).longValue();
        }
        if (v instanceof String) {
            try {
                return Long.parseLong((String) v);
            } catch (NumberFormatException e) {
                // fall through
            }
        }
        throw new BadRequestException(where + " must be an integer");
    }

    private static long optLong(Object v, long dflt) {
        return v == null ? dflt : asLong(v, "numeric field");
    }

    private static Long optLong(Object v, Long dflt) {
        return v == null ? dflt : asLong(v, "numeric field");
    }

    private static boolean optBool(Object v, boolean dflt) {
        if (v == null) {
            return dflt;
        }
        if (v instanceof Boolean) {
            return (Boolean) v;
        }
        throw new BadRequestException("boolean field expected");
    }

    /** 请求内容/语义错误（映射 HTTP 400）。 */
    public static class BadRequestException extends RuntimeException {
        public BadRequestException(String message) {
            super(message);
        }
    }

    /**
     * 容量超限（映射 HTTP 422）：请求本身合法，但模拟过程中缓冲超过配置上限。
     */
    public static class CapacityException extends RuntimeException {
        public CapacityException(BufferCapacityExceededException cause) {
            super(cause.getMessage(), cause);
        }
    }
}
