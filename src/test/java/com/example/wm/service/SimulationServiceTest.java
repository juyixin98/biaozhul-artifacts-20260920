package com.example.wm.service;

import com.example.wm.json.Json;
import com.example.wm.json.JsonException;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 通过 JSON 脚本跑通“暂停—快分区推进—恢复旧事件迟到—全局不倒退”的完整验收链路。
 */
class SimulationServiceTest {

    private final SimulationService service = new SimulationService();

    @Test
    void fullAcceptanceScript_overJson() {
        String request = """
                {
                  "boundMs": 100,
                  "idleTimeoutMs": 500,
                  "startTimeMs": 0,
                  "actions": [
                    {"type": "event", "time": 0,  "partition": "a", "eventTime": 1000},
                    {"type": "event", "time": 10, "partition": "b", "eventTime": 1020},
                    {"type": "pause", "time": 20, "partition": "a"},
                    {"type": "event", "time": 30, "partition": "b", "eventTime": 5000},
                    {"type": "event", "time": 40, "partition": "a", "eventTime": 1500, "payload": "replayed-old"},
                    {"type": "event", "time": 50, "partition": "a", "eventTime": 6000},
                    {"type": "event", "time": 60, "partition": "b", "eventTime": 7000},
                    {"type": "event", "time": 70, "partition": "a", "eventTime": 7100}
                  ]
                }
                """;
        String responseJson = service.executeJson(request);
        @SuppressWarnings("unchecked")
        Map<String, Object> resp = (Map<String, Object>) Json.parse(responseJson);

        assertEquals(Boolean.TRUE, resp.get("ok"));
        // 最终全局水位线 = min(a: 7100-100=7000, b: 7000-100=6900) = 6900
        assertEquals(6900L, resp.get("globalWatermark"));

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> steps = (List<Map<String, Object>>) resp.get("steps");

        // step4: a 恢复重放旧事件
        var recoveredStep = steps.get(4);
        assertEquals("LATE", recoveredStep.get("classification"));
        assertEquals(Boolean.TRUE, recoveredStep.get("resumedFromIdle"));
        assertEquals(Boolean.FALSE, recoveredStep.get("accepted"));
        assertEquals(4900L, recoveredStep.get("globalWatermark"),
                "恢复落后分区后全局水位线保持 4900，不倒退");
        assertEquals(4900L, recoveredStep.get("previousGlobalWatermark"));

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> lates = (List<Map<String, Object>>) resp.get("lateEvents");
        assertEquals(1, lates.size());
        assertEquals("RECOVERED_OLD", lates.get(0).get("reason"));
        assertEquals("a", lates.get(0).get("partition"));
        assertEquals(1500L, lates.get(0).get("eventTimeMs"));
        assertEquals("replayed-old", lates.get(0).get("payload"));
        assertEquals(4900L, lates.get(0).get("globalWatermarkMs"));

        @SuppressWarnings("unchecked")
        Map<String, Object> snapshot = (Map<String, Object>) resp.get("snapshot");
        assertEquals(70L, snapshot.get("processingTimeMs"));
        assertEquals(6900L, snapshot.get("globalWatermark"));
        assertEquals(2L, ((Number) snapshot.get("activeCount")).longValue());
    }

    @Test
    void idleTimeoutBoundary_overJson() {
        String request = """
                {
                  "boundMs": 0,
                  "idleTimeoutMs": 100,
                  "actions": [
                    {"type": "event", "time": 0, "partition": "a", "eventTime": 100},
                    {"type": "event", "time": 0, "partition": "b", "eventTime": 200},
                    {"type": "tick", "time": 99},
                    {"type": "tick", "time": 100}
                  ]
                }
                """;
        @SuppressWarnings("unchecked")
        Map<String, Object> resp = (Map<String, Object>) service.execute(Json.parseObject(request));
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> steps = (List<Map<String, Object>>) resp.get("steps");
        assertTrue(((List<?>) steps.get(2).get("timedOutPartitions")).isEmpty(), "t=99 不空闲");
        var atBoundary = (List<?>) steps.get(3).get("timedOutPartitions");
        assertEquals(2, atBoundary.size(), "t=100 恰好边界，两分区同时空闲");
        // 全空闲后全局水位线保留：min(100,200)=100
        assertEquals(100L, steps.get(3).get("globalWatermark"));
    }

    @Test
    void extremeFastPartition_overJson_pinnedBySlow() {
        StringBuilder actions = new StringBuilder();
        actions.append("""
                {"type":"event","time":0,"partition":"slow","eventTime":10},
                """);
        for (int t = 1000; t <= 20000; t += 1000) {
            actions.append("{\"type\":\"event\",\"time\":")
                    .append(t / 10).append(",\"partition\":\"fast\",\"eventTime\":")
                    .append(t).append("},");
        }
        actions.append("{\"type\":\"pause\",\"time\":3000,\"partition\":\"slow\"},");
        actions.append("{\"type\":\"event\",\"time\":3001,\"partition\":\"fast\",\"eventTime\":99999}");
        String request = "{\"boundMs\":0,\"idleTimeoutMs\":100000,\"actions\":[" + actions + "]}";

        @SuppressWarnings("unchecked")
        Map<String, Object> resp = (Map<String, Object>) service.execute(Json.parseObject(request));
        assertEquals(99999L, resp.get("globalWatermark"));
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> steps = (List<Map<String, Object>>) resp.get("steps");
        for (int i = 1; i <= 20; i++) {
            assertEquals(10L, steps.get(i).get("globalWatermark"),
                    "慢分区停滞期间全局水位线必须钉在 10，step=" + i);
        }
    }

    @Test
    void deterministic_sameRequestSameResponse() {
        String request = """
                {"boundMs":5,"idleTimeoutMs":50,"actions":[
                  {"type":"event","partition":"a","eventTime":100},
                  {"type":"advance","durationMs":60},
                  {"type":"tick"},
                  {"type":"event","partition":"a","eventTime":120}
                ]}
                """;
        String r1 = service.executeJson(request);
        String r2 = service.executeJson(request);
        assertEquals(r1, r2, "虚拟时钟重放必须完全确定");
    }

    @Test
    void badRequest_reportsError() {
        assertThrows(JsonException.class, () -> service.executeJson("{not json"));
        assertThrows(JsonException.class, () -> service.executeJson("{\"actions\":[]}".replace("[]",
                "[{\"type\":\"bogus\"}]")));
        var ex = assertThrows(JsonException.class,
                () -> service.executeJson("{\"boundMs\":0,\"idleTimeoutMs\":10,\"actions\":["
                        + "{\"type\":\"event\",\"partition\":\"a\"}]}"));
        assertTrue(ex.getMessage().contains("eventTime"));
        // bound 不允许负数
        assertThrows(IllegalArgumentException.class,
                () -> service.executeJson("{\"boundMs\":-1,\"idleTimeoutMs\":10,\"actions\":[]}"));
    }

    @Test
    void missingActions_rejected() {
        assertThrows(JsonException.class, () -> service.executeJson("{}"));
    }
}
