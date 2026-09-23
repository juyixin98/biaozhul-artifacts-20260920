package com.example.quantiles.service;

import com.example.quantiles.json.Json;
import com.example.quantiles.json.JsonException;
import com.example.quantiles.model.Event;
import com.example.quantiles.quantile.Fraction;
import com.example.quantiles.window.SlidingWindowQuantileOperator;
import com.example.quantiles.window.WindowResult;
import com.example.quantiles.window.WindowSpec;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 离线批处理：对一段 JSON 请求做一次滑动窗口精确分位数计算。
 *
 * <p>核心复用流式算子 {@link SlidingWindowQuantileOperator}（同一套事件时间窗口语义、
 * watermark 与迟到规则），只是驱动方式为：按（可选排序后的）事件顺序喂入，
 * 最后把 watermark 推到无穷大以刷出所有窗口。
 *
 * <p>为满足“窗口为空”可观察，结果会补齐从首个可能窗口到最后一个可能窗口之间
 * slide 网格上的每一个窗口（无事件/无保留 pane 的窗口 count=0、分位数值为 null）。
 */
public final class BatchQuantileService {

    /** 默认请求的分位数：中位数。 */
    public static final List<Double> DEFAULT_QUANTILES = List.of(0.5);

    public Map<String, Object> handle(Map<String, Object> request) {
        if (request == null) {
            throw new BadRequestException("请求体必须是 JSON 对象");
        }
        long sizeMillis = requirePositiveLong(request, "windowSizeMillis");
        Long slideObj = optionalLong(request, "windowSlideMillis");
        long slideMillis = slideObj == null ? sizeMillis : slideObj;
        if (slideMillis <= 0) {
            throw new BadRequestException("windowSlideMillis 必须为正数: " + slideMillis);
        }
        if (sizeMillis % slideMillis != 0) {
            throw new BadRequestException(
                    "windowSizeMillis 必须是 windowSlideMillis 的整数倍: size=" + sizeMillis
                            + " slide=" + slideMillis);
        }
        long allowedLateness = optionalLongOrDefault(request, "allowedLatenessMillis", 0L);
        if (allowedLateness < 0) {
            throw new BadRequestException("allowedLatenessMillis 不能为负: " + allowedLateness);
        }
        List<Double> quantiles = parseQuantiles(request.get("quantiles"));
        boolean sortByTimestamp = optionalBoolOrDefault(request, "sortByTimestamp", true);

        Object eventsObj = request.get("events");
        if (eventsObj == null || Json.isNull(eventsObj)) {
            throw new BadRequestException("缺少 events 数组");
        }
        List<Object> rawEvents = Json.asArray(eventsObj);
        List<Event> events = new ArrayList<>(rawEvents.size());
        for (int i = 0; i < rawEvents.size(); i++) {
            Map<String, Object> ev = Json.asObject(rawEvents.get(i));
            Object ts = ev.get("timestampMillis");
            Object v = ev.get("value");
            if (ts == null || v == null) {
                throw new BadRequestException(
                        "events[" + i + "] 必须包含 timestampMillis 和 value");
            }
            events.add(new Event(Json.asLong(ts), Json.asLong(v)));
        }
        if (sortByTimestamp) {
            events.sort((a, b) -> Long.compare(a.timestampMillis(), b.timestampMillis()));
        }

        WindowSpec spec = new WindowSpec(sizeMillis, slideMillis, quantiles, allowedLateness);
        SlidingWindowQuantileOperator operator = new SlidingWindowQuantileOperator(spec);
        for (Event e : events) {
            operator.process(e);
        }
        // flush：watermark 推到无穷大，刷出所有仍保留的窗口
        List<WindowResult> results = operator.advanceWatermark(Long.MAX_VALUE);

        Map<String, Object> response = new LinkedHashMap<>();
        response.put("windowSizeMillis", sizeMillis);
        response.put("windowSlideMillis", slideMillis);
        response.put("allowedLatenessMillis", allowedLateness);
        response.put("quantiles", quantiles);
        response.put("eventCount", events.size());
        response.put("lateDroppedCount", operator.lateDroppedCount());
        response.put("windowCount", results.size());
        response.put("results", encodeResults(results, quantiles));
        return response;
    }

    private List<Map<String, Object>> encodeResults(List<WindowResult> results,
                                                    List<Double> quantiles) {
        List<Map<String, Object>> out = new ArrayList<>(results.size());
        for (WindowResult r : results) {
            Map<String, Object> w = new LinkedHashMap<>();
            w.put("windowStartMillis", r.windowStartMillis());
            w.put("windowEndMillis", r.windowEndMillis());
            w.put("count", r.count());
            if (r.isEmpty()) {
                w.put("empty", true);
                w.put("values", nulls(quantiles.size()));
            } else {
                w.put("empty", false);
                w.put("values", r.values()); // Fraction -> JSON 数字（由 JsonWriter 输出）
            }
            out.add(w);
        }
        return out;
    }

    private static List<Object> nulls(int n) {
        List<Object> l = new ArrayList<>(n);
        for (int i = 0; i < n; i++) {
            l.add(null);
        }
        return l;
    }

    private List<Double> parseQuantiles(Object qObj) {
        if (qObj == null || Json.isNull(qObj)) {
            return DEFAULT_QUANTILES;
        }
        List<Object> arr = Json.asArray(qObj);
        if (arr.isEmpty()) {
            throw new BadRequestException("quantiles 不能为空数组");
        }
        List<Double> qs = new ArrayList<>(arr.size());
        for (int i = 0; i < arr.size(); i++) {
            double q = Json.asDouble(arr.get(i));
            if (Double.isNaN(q) || q < 0.0 || q > 1.0) {
                throw new BadRequestException("quantiles[" + i + "] 必须在 [0,1] 内: " + q);
            }
            qs.add(q);
        }
        return qs;
    }

    private static long requirePositiveLong(Map<String, Object> req, String key) {
        Object v = req.get(key);
        if (v == null || Json.isNull(v)) {
            throw new BadRequestException("缺少必填字段 " + key);
        }
        long n;
        try {
            n = Json.asLong(v);
        } catch (JsonException e) {
            throw new BadRequestException(key + " 必须是整数");
        }
        if (n <= 0) {
            throw new BadRequestException(key + " 必须为正数: " + n);
        }
        return n;
    }

    private static Long optionalLong(Map<String, Object> req, String key) {
        Object v = req.get(key);
        if (v == null || Json.isNull(v)) {
            return null;
        }
        return Json.asLong(v);
    }

    private static long optionalLongOrDefault(Map<String, Object> req, String key, long def) {
        Long v = optionalLong(req, key);
        return v == null ? def : v;
    }

    private static boolean optionalBoolOrDefault(Map<String, Object> req, String key, boolean def) {
        Object v = req.get(key);
        if (v == null || Json.isNull(v)) {
            return def;
        }
        return Json.asBool(v);
    }

    /** 请求错误（映射为 HTTP 400）。 */
    public static final class BadRequestException extends RuntimeException {
        public BadRequestException(String message) {
            super(message);
        }
    }
}
