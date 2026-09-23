package com.example.watermark.test;

import static com.example.watermark.test.Assert.assertEquals;
import static com.example.watermark.test.Assert.assertFalse;
import static com.example.watermark.test.Assert.assertTrue;

import java.util.List;
import java.util.Map;

import com.example.watermark.json.Json;
import com.example.watermark.service.WatermarkSession;
import com.example.watermark.test.TestRunner.Test;

/**
 * End-to-end acceptance runs through the JSON script service: pause/resume,
 * extreme fast partition, and window output — the same payloads shipped under
 * {@code examples/}.
 */
@SuppressWarnings("unchecked")
public class ScenarioTest {

    private Map<String, Object> run(String resourceJson) {
        Map<String, Object> request = Json.parseObject(resourceJson);
        return WatermarkSession.runStandalone(request);
    }

    @Test
    public void pauseResumeScenarioFromExample() {
        Map<String, Object> out = run(Examples.PAUSE_RESUME);
        Map<String, Object> snapshot = (Map<String, Object>) out.get("snapshot");
        assertEquals(3500L, ((Number) snapshot.get("globalWatermark")).longValue(),
                "global wm advanced to 3500");

        List<Map<String, Object>> late = (List<Map<String, Object>>) out.get("lateEvents");
        assertEquals(1, late.size(), "exactly one late replay event");
        assertEquals(500L, ((Number) late.get(0).get("timestamp")).longValue());
        assertEquals(Boolean.TRUE, late.get(0).get("fromResumedPartition"));

        Map<String, Object> partitions = (Map<String, Object>) snapshot.get("partitions");
        assertEquals("ACTIVE", ((Map<String, Object>) partitions.get("p2")).get("state"));
    }

    @Test
    public void fastPartitionScenarioFromExample() {
        Map<String, Object> out = run(Examples.FAST_PARTITION);
        Map<String, Object> snapshot = (Map<String, Object>) out.get("snapshot");
        // slow reaches 30; fast jumps to billions.
        assertEquals(30L, ((Number) snapshot.get("globalWatermark")).longValue(),
                "extreme fast partition cannot advance the global watermark past slow");
    }

    @Test
    public void windowScenarioClosesExactWindows() {
        Map<String, Object> out = run(Examples.WINDOWS);
        List<Map<String, Object>> windows = (List<Map<String, Object>>) out.get("windows");
        // p1: [0,1000) closes with 2 events; p2: [0,1000) closes with 1.
        assertEquals(2, windows.size());
        int totalEvents = windows.stream()
                .mapToInt(w -> ((Number) w.get("eventCount")).intValue()).sum();
        assertEquals(3, totalEvents);

        List<Map<String, Object>> late = (List<Map<String, Object>>) out.get("lateEvents");
        assertTrue(late.stream().anyMatch(l -> ((Number) l.get("timestamp")).longValue() == 50),
                "post-close timestamp 50 is late");
    }

    @Test
    public void malformedScriptIsRejected() {
        boolean threw = false;
        try {
            run("{\"config\":{},\"steps\":\"nope\"}");
        } catch (IllegalArgumentException e) {
            threw = true;
        }
        assertTrue(threw, "non-array steps rejected");
    }

    @Test
    public void everyObservedGlobalWatermarkAcrossScenarioIsMonotonic() {
        Map<String, Object> out = run(Examples.PAUSE_RESUME);
        long prev = Long.MIN_VALUE;
        for (Object raw : (List<?>) out.get("steps")) {
            Map<String, Object> step = (Map<String, Object>) raw;
            Object wm = step.get("globalWatermark");
            if (wm != null) {
                long w = ((Number) wm).longValue();
                assertTrue(w >= prev, "regression at step " + step + " in monotonic check");
                prev = w;
            }
        }
        assertFalse(prev == Long.MIN_VALUE, "some watermark was emitted");
    }
}
