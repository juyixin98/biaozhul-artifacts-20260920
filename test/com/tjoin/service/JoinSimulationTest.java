package com.tjoin.service;

import static com.tjoin.Asserts.assertEquals;
import static com.tjoin.Asserts.assertThrows;
import static com.tjoin.Asserts.assertTrue;

import com.tjoin.Test;

import java.util.List;
import java.util.Map;

@SuppressWarnings("unchecked")
public class JoinSimulationTest {

    private Map<String, Object> run(String json) {
        return JoinSimulation.run(Json.parseObject(json));
    }

    @Test
    public void simpleModeJoinsWithinInterval() {
        Map<String, Object> resp = run("""
                {
                  "config": {"lowerBound": -2, "upperBound": 2},
                  "left":  [
                    {"id":"L1","key":"a","ts":10,"value":"click"},
                    {"id":"L2","key":"a","ts":11,"value":"click"}
                  ],
                  "right": [
                    {"id":"R1","key":"a","ts":10,"value":"imp"},
                    {"id":"R2","key":"a","ts":13,"value":"imp"}
                  ]
                }
                """);
        List<Map<String, Object>> results = (List<Map<String, Object>>) resp.get("results");
        // L10 配 R10；R13 与 L10 差 3、与 L11 差 2 -> 只配 L11；R10 同时配 L11
        // pairs: (L1,R1), (L2,R1), (L2 与 R2 diff2) = 3
        assertEquals(3, results.size(), "3 pairs");
        Map<String, Object> metrics = (Map<String, Object>) resp.get("metrics");
        assertEquals(3L, ((Number) metrics.get("emitted")).longValue(), "emitted metric");
    }

    @Test
    public void stepsModeDemonstratesStalledSideAndNoEarlyDrop() {
        Map<String, Object> resp = run("""
                {
                  "config": {"lowerBound": 0, "upperBound": 5},
                  "steps": [
                    {"type":"event","side":"LEFT","id":"L1","key":"k","ts":10},
                    {"type":"event","side":"LEFT","id":"L2","key":"k","ts":12},
                    {"type":"watermark","side":"LEFT","watermark":1000},
                    {"type":"event","side":"RIGHT","id":"R1","key":"k","ts":15},
                    {"type":"watermark","side":"RIGHT","watermark":15},
                    {"type":"watermark","side":"RIGHT","watermark":16}
                  ]
                }
                """);
        List<Map<String, Object>> results = (List<Map<String, Object>>) resp.get("results");
        // 右流停滞期间 L1/L2 保留；R1@15 同时匹配两者（L1 上界闭区间）
        assertEquals(2, results.size(), "stalled right: both lefts still match R1");

        List<Map<String, Object>> trace = (List<Map<String, Object>>) resp.get("trace");
        Map<String, Object> finalStep = trace.get(trace.size() - 1);
        // 右水位线 16：L1(10) 上界 15 < 16 过期；L2(12) 上界 17 保留
        Map<String, Object> buffered = (Map<String, Object>) finalStep.get("buffered");
        assertEquals(1L, ((Number) buffered.get("left")).longValue(), "only L1 expired at wmR=16");
        assertEquals(0L, ((Number) buffered.get("right")).longValue(), "R1 expired by left wm=1000 already");
    }

    @Test
    public void duplicateIdsAndLateEventsReportedInMetrics() {
        Map<String, Object> resp = run("""
                {
                  "config": {"lowerBound": 0, "upperBound": 10},
                  "steps": [
                    {"type":"event","side":"LEFT","id":"L1","key":"k","ts":10},
                    {"type":"event","side":"LEFT","id":"L1","key":"k","ts":10},
                    {"type":"watermark","side":"LEFT","watermark":20},
                    {"type":"event","side":"LEFT","id":"Llate","key":"k","ts":19},
                    {"type":"event","side":"LEFT","id":"Lok","key":"k","ts":20}
                  ]
                }
                """);
        Map<String, Object> metrics = (Map<String, Object>) resp.get("metrics");
        assertEquals(1L, ((Number) metrics.get("duplicates")).longValue(), "one duplicate");
        assertEquals(1L, ((Number) metrics.get("lateDropped")).longValue(), "one late drop");
    }

    @Test
    public void capacityOverflowRejectedAsCapacityError() {
        JoinSimulation.CapacityException ex = assertThrows(
                JoinSimulation.CapacityException.class,
                () -> run("""
                        {
                          "config": {"lowerBound": 0, "upperBound": 100,
                                     "maxBufferedPerSide": 2},
                          "steps": [
                            {"type":"event","side":"LEFT","id":"L1","key":"k","ts":1},
                            {"type":"event","side":"LEFT","id":"L2","key":"k","ts":2},
                            {"type":"event","side":"LEFT","id":"L3","key":"k","ts":3}
                          ]
                        }
                        """),
                "buffer cap must surface as CapacityException");
        assertTrue(ex.getMessage().contains("LEFT"), "message identifies side");
    }

    @Test
    public void badRequestsAreRejected() {
        assertThrows(JoinSimulation.BadRequestException.class,
                () -> run("{\"left\":[]}"), "missing config");
        assertThrows(JoinSimulation.BadRequestException.class,
                () -> run("{\"config\":{\"lowerBound\":5,\"upperBound\":1}}"),
                "lower > upper rejected");
        assertThrows(JoinSimulation.BadRequestException.class,
                () -> run("{\"config\":{\"lowerBound\":0,\"upperBound\":1},\"steps\":["
                        + "{\"type\":\"event\",\"side\":\"UP\",\"id\":\"x\",\"key\":\"k\",\"ts\":1}]}"),
                "invalid side");
    }

    @Test
    public void advanceWatermarkDrainsStateInSimpleMode() {
        Map<String, Object> resp = run("""
                {
                  "config": {"lowerBound": 0, "upperBound": 3},
                  "left":  [{"id":"L1","key":"k","ts":10}],
                  "right": [{"id":"R1","key":"k","ts":13}],
                  "advanceSide": "RIGHT",
                  "advanceWatermarkTo": 100
                }
                """);
        List<Map<String, Object>> results = (List<Map<String, Object>>) resp.get("results");
        assertEquals(1, results.size(), "boundary pair produced before expiry");
        Map<String, Object> buffered = (Map<String, Object>) resp.get("buffered");
        assertEquals(0L, ((Number) buffered.get("left")).longValue(), "left drained after big right wm");
    }
}
