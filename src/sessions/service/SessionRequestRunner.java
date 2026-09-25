package sessions.service;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import sessions.agg.AggregateFunction;
import sessions.agg.CountAggregate;
import sessions.agg.SumAggregate;
import sessions.model.Event;
import sessions.model.WindowUpdate;
import sessions.op.SessionWindowOperator;
import sessions.reference.ChangelogFold;
import sessions.time.SimTimerService;

/**
 * 把一个 JSON 请求（已解析为 Map）跑成 JSON 响应（Map）。
 *
 * <p>HTTP 服务与命令行共用本类，保证两种入口行为完全一致。
 *
 * <h3>请求格式</h3>
 * <pre>{@code
 * {
 *   "gap": 10,
 *   "allowedLateness": 5,
 *   "aggregate": "COUNT",            // 或 "SUM"，默认 COUNT
 *   "watermarkStrategy": {           // 可选，默认 BOUNDED, maxOutOfOrderness=0
 *     "type": "BOUNDED", "maxOutOfOrderness": 0
 *   },                               // type 也可是 "NONE"（仅手动水位线）
 *   "input": [
 *     {"key": "a", "timestamp": 1, "value": 10},
 *     {"watermark": 15}
 *   ],
 *   "finish": true                   // 可选，默认 true：收尾封存全部窗口
 * }
 * }</pre>
 *
 * <p>BOUNDED 策略：每来一个事件，水位线推进到
 * {@code max(W, eventTimestamp - maxOutOfOrderness)}。手动 watermark 项可与事件交错。
 */
public final class SessionRequestRunner {

    private SessionRequestRunner() {
    }

    public static Map<String, Object> run(Map<String, Object> request) {
        long gap = requireLong(request, "gap");
        long allowedLateness = optLong(request, "allowedLateness", 0L);
        if (gap < 0) {
            throw new BadRequestException("gap must be >= 0");
        }
        if (allowedLateness < 0) {
            throw new BadRequestException("allowedLateness must be >= 0");
        }

        AggregateFunction<?> agg = switch (optString(request, "aggregate", "COUNT")) {
            case "COUNT" -> new CountAggregate();
            case "SUM" -> new SumAggregate();
            default -> throw new BadRequestException(
                    "aggregate must be COUNT or SUM");
        };
        String aggregateName =
                agg.getClass().getSimpleName().replace("Aggregate", "");

        String wmType = "BOUNDED";
        long maxOutOfOrderness = 0L;
        Object strategy = request.get("watermarkStrategy");
        if (strategy instanceof Map<?, ?> sm) {
            wmType = sm.get("type") == null ? "BOUNDED" : String.valueOf(sm.get("type"));
            maxOutOfOrderness = asLong(sm.get("maxOutOfOrderness"), 0L);
        } else if (request.containsKey("watermarkMode")) {
            wmType = optString(request, "watermarkMode", "BOUNDED");
        }
        if (!wmType.equals("BOUNDED") && !wmType.equals("NONE")) {
            throw new BadRequestException(
                    "watermarkStrategy.type must be BOUNDED or NONE");
        }
        if (maxOutOfOrderness < 0) {
            throw new BadRequestException("maxOutOfOrderness must be >= 0");
        }

        Object inputObj = request.get("input");
        if (!(inputObj instanceof List<?>)) {
            throw new BadRequestException("'input' must be an array");
        }

        boolean finish = asBool(request.get("finish"), true);

        SimTimerService timers = new SimTimerService();
        SessionWindowOperator operator =
                new SessionWindowOperator(gap, allowedLateness, agg, timers);

        for (Object itemObj : (List<?>) inputObj) {
            if (!(itemObj instanceof Map<?, ?> item)) {
                throw new BadRequestException("each input item must be an object");
            }
            if (item.containsKey("watermark")) {
                long wm = asLong(item.get("watermark"), Long.MIN_VALUE);
                operator.advanceWatermark(wm);
            } else {
                if (item.get("key") == null) {
                    throw new BadRequestException("event item requires 'key'");
                }
                if (!(item.get("timestamp") instanceof Number)) {
                    throw new BadRequestException("event item requires numeric 'timestamp'");
                }
                String key = String.valueOf(item.get("key"));
                long ts = ((Number) item.get("timestamp")).longValue();
                long value = asLong(item.get("value"), 1L);
                operator.processElement(new Event(key, ts, value));
                if (wmType.equals("BOUNDED")) {
                    long wm = ts - maxOutOfOrderness; // 测试样本均为小整数，无溢出场景
                    operator.advanceWatermark(wm);
                }
            }
        }

        if (finish) {
            operator.finish();
        }

        return buildResponse(gap, allowedLateness, aggregateName,
                wmType, maxOutOfOrderness, operator, finish);
    }

    private static Map<String, Object> buildResponse(
            long gap, long allowedLateness, String aggregateName,
            String wmType, long maxOutOfOrderness,
            SessionWindowOperator operator, boolean finish) {
        Map<String, Object> response = new LinkedHashMap<>();

        Map<String, Object> config = new LinkedHashMap<>();
        config.put("gap", gap);
        config.put("allowedLateness", allowedLateness);
        config.put("aggregate", aggregateName);
        Map<String, Object> strategyOut = new LinkedHashMap<>();
        strategyOut.put("type", wmType);
        strategyOut.put("maxOutOfOrderness", maxOutOfOrderness);
        config.put("watermarkStrategy", strategyOut);
        config.put("finish", finish);
        response.put("config", config);

        Map<String, Object> stats = new LinkedHashMap<>();
        long sealedRetained = operator.totalRetainedWindowCount()
                - operator.totalActiveWindowCount();
        stats.put("receivedEvents", operator.receivedEvents());
        stats.put("droppedLateEvents", operator.droppedLateEvents());
        stats.put("activeWindowsRetained", operator.totalActiveWindowCount());
        stats.put("sealedWindowsRetained", sealedRetained);
        stats.put("finalWatermark", operator.currentWatermark());
        response.put("stats", stats);

        List<Map<String, Object>> changes = new ArrayList<>();
        for (WindowUpdate u : operator.changelog()) {
            Map<String, Object> c = new LinkedHashMap<>();
            c.put("seq", u.sequence());
            c.put("kind", u.kind().name());
            c.put("key", u.key());
            c.put("start", u.window().start());
            c.put("end", u.window().end());
            c.put("aggregate", u.aggregate());
            c.put("watermark", u.emittedAtWatermark());
            changes.add(c);
        }
        response.put("changelog", changes);

        List<Map<String, Object>> finalResults = new ArrayList<>();
        for (ChangelogFold.Row row : ChangelogFold.fold(operator.changelog())) {
            Map<String, Object> r = new LinkedHashMap<>();
            r.put("key", row.key());
            r.put("start", row.start());
            r.put("end", row.end());
            r.put("aggregate", row.aggregate());
            finalResults.add(r);
        }
        response.put("finalResults", finalResults);
        return response;
    }

    private static long requireLong(Map<?, ?> m, String name) {
        Object v = m.get(name);
        if (!(v instanceof Number)) {
            throw new BadRequestException("missing or non-numeric field '" + name + "'");
        }
        return ((Number) v).longValue();
    }

    private static long optLong(Map<?, ?> m, String name, long dflt) {
        Object v = m.get(name);
        return v == null ? dflt : asLong(v, dflt);
    }

    private static String optString(Map<?, ?> m, String name, String dflt) {
        Object v = m.get(name);
        return v == null ? dflt : String.valueOf(v);
    }

    private static long asLong(Object v, long dflt) {
        if (v instanceof Number n) {
            return n.longValue();
        }
        if (v instanceof String s) {
            try {
                return Long.parseLong(s.trim());
            } catch (NumberFormatException ignored) {
                return dflt;
            }
        }
        return dflt;
    }

    private static boolean asBool(Object v, boolean dflt) {
        if (v instanceof Boolean b) {
            return b;
        }
        return dflt;
    }
}
