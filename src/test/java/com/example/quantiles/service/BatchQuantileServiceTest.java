package com.example.quantiles.service;

import com.example.quantiles.json.Json;
import com.example.quantiles.json.JsonParser;
import com.example.quantiles.json.JsonWriter;
import org.junit.jupiter.api.Test;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class BatchQuantileServiceTest {

    private final BatchQuantileService service = new BatchQuantileService();

    private Map<String, Object> req(String json) {
        return Json.asObject(JsonParser.parse(json));
    }

    private Map<String, Object> ev(long t, long v) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("timestampMillis", t);
        m.put("value", v);
        return m;
    }

    @Test
    void computesMedianPerWindowAgainstExpected() {
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("windowSizeMillis", 10);
        r.put("windowSlideMillis", 10);
        r.put("quantiles", List.of(0.5));
        r.put("events", List.of(ev(1, 1), ev(2, 2), ev(3, 3),
                ev(11, 10), ev(12, 20)));
        Map<String, Object> out = service.handle(r);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> results = (List<Map<String, Object>>) out.get("results");
        assertEquals(2, results.size());
        assertEquals(3L, Json.asLong(results.get(0).get("count")));
        assertEquals("2", JsonWriter.write(((List<?>) results.get(0).get("values")).get(0)));
        assertEquals(2L, Json.asLong(results.get(1).get("count")));
        // 第二窗 [10,20): [10,20] median 15
        Object med = ((List<?>) results.get(1).get("values")).get(0);
        assertEquals("15", JsonWriter.write(med));
    }

    @Test
    void negativeValuesAndAllDuplicates() {
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("windowSizeMillis", 5);
        r.put("events", List.of(ev(0, -7), ev(1, -7), ev(2, -7), ev(3, -7)));
        Map<String, Object> out = service.handle(r);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> results = (List<Map<String, Object>>) out.get("results");
        assertEquals(4L, Json.asLong(results.get(0).get("count")));
        assertEquals("-7", JsonWriter.write(((List<?>) results.get(0).get("values")).get(0)));
    }

    @Test
    void emptyEventsReturnsZeroWindows() {
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("windowSizeMillis", 10);
        r.put("events", List.of());
        Map<String, Object> out = service.handle(r);
        assertEquals(0L, ((Number) out.get("windowCount")).longValue());
        assertEquals(List.of(), out.get("results"));
    }

    @Test
    void emptyWindowsAreReportedWhenGapsExist() {
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("windowSizeMillis", 10);
        r.put("events", List.of(ev(0, 1), ev(25, 2)));
        Map<String, Object> out = service.handle(r);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> results = (List<Map<String, Object>>) out.get("results");
        assertEquals(3, results.size());
        assertEquals(Boolean.FALSE, results.get(0).get("empty"));
        assertEquals(Boolean.TRUE, results.get(1).get("empty"));
        assertEquals(Boolean.FALSE, results.get(2).get("empty"));
        assertNull(((List<?>) results.get(1).get("values")).get(0));
    }

    @Test
    void sameTimestampDuplicatesCountFully() {
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("windowSizeMillis", 10);
        r.put("events", List.of(ev(5, 1), ev(5, 2), ev(5, 3), ev(5, 4)));
        Map<String, Object> out = service.handle(r);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> results = (List<Map<String, Object>>) out.get("results");
        assertEquals(4L, Json.asLong(results.get(0).get("count")));
        // [1,2,3,4] median 2.5
        assertEquals("2.5",
                JsonWriter.write(((List<?>) results.get(0).get("values")).get(0)));
    }

    @Test
    void badRequestsAreRejectedWith400StyleErrors() {
        Map<String, Object> base = new LinkedHashMap<>();
        base.put("windowSizeMillis", 10);
        base.put("events", List.of());

        Map<String, Object> noSize = new LinkedHashMap<>(base);
        noSize.remove("windowSizeMillis");
        assertThrows(BatchQuantileService.BadRequestException.class, () -> service.handle(noSize));

        Map<String, Object> badSlide = new LinkedHashMap<>(base);
        badSlide.put("windowSlideMillis", 3);
        assertThrows(BatchQuantileService.BadRequestException.class,
                () -> service.handle(badSlide)); // 10 不是 3 的倍数

        Map<String, Object> badQ = new LinkedHashMap<>(base);
        badQ.put("quantiles", List.of(1.5));
        assertThrows(BatchQuantileService.BadRequestException.class, () -> service.handle(badQ));

        Map<String, Object> missingField = new LinkedHashMap<>();
        missingField.put("windowSizeMillis", 10);
        missingField.put("events", List.of(Map.of("timestampMillis", 1)));
        assertThrows(BatchQuantileService.BadRequestException.class,
                () -> service.handle(missingField));
    }

    @Test
    void outOfOrderEventsAreSortedByDefaultAndNothingDroppedInBatch() {
        // 默认按时间戳排序：即使 JSON 中乱序给出，窗口仍完整
        Map<String, Object> sortedReq = new LinkedHashMap<>();
        sortedReq.put("windowSizeMillis", 10);
        sortedReq.put("events", List.of(ev(8, 1), ev(1, 5)));
        Map<String, Object> out1 = service.handle(sortedReq);
        assertEquals(0L, out1.get("lateDroppedCount"));
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> results =
                (List<Map<String, Object>>) out1.get("results");
        assertEquals(2L, Json.asLong(results.get(0).get("count")), "乱序事件排序后应同处 [0,10) 窗口");

        // 批处理在喂完所有事件后才把 watermark 推到无穷，因此不存在中途触发，
        // 即使关闭排序也不会判迟到（迟到语义只在流式逐事件 watermark 下出现）
        Map<String, Object> rawReq = new LinkedHashMap<>();
        rawReq.put("windowSizeMillis", 10);
        rawReq.put("sortByTimestamp", false);
        rawReq.put("events", List.of(ev(1, 5), ev(15, 9), ev(2, 1)));
        Map<String, Object> out2 = service.handle(rawReq);
        assertEquals(0L, out2.get("lateDroppedCount"));
        assertTrue(((Number) out2.get("windowCount")).longValue() >= 1);
    }
}
