package com.example.migration.json;

import com.example.migration.model.Edge;
import com.example.migration.model.PlanRequest;
import com.example.migration.model.PlanResponse;
import com.example.migration.service.PlanningService;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class JsonRoundTripTest {

    @Test
    @DisplayName("PlanRequest with instants/preconditions round-trips through JSON")
    void requestRoundTrip() throws Exception {
        String json = """
                {
                  "from": "v2",
                  "to": "v4",
                  "at": "2026-06-01T02:00:00Z",
                  "mode": "UPGRADE",
                  "attributes": {"region": "cn-east", "maintenance_window": true},
                  "requireReversible": false,
                  "maxPaths": 2,
                  "graph": {
                    "nodes": ["v2", "v4"],
                    "edges": [
                      {"from": "v2", "to": "v4", "cost": 3.5, "reversible": true,
                       "preconditions": {"region": ["cn-east", "cn-north"]},
                       "validFrom": "2026-01-01T00:00:00Z",
                       "validTo": "2026-12-31T23:59:59Z",
                       "description": "json edge"}
                    ]
                  }
                }
                """;
        PlanRequest req = Json.mapper().readValue(json, PlanRequest.class);
        assertEquals(Instant.parse("2026-06-01T02:00:00Z"), req.at());
        assertEquals("cn-east", req.attributes().get("region"));
        assertEquals(PlanRequest.Mode.UPGRADE, req.mode());
        Edge edge = req.graph().edges().get(0);
        assertEquals(3.5, edge.cost(), 1e-9);
        assertEquals(List.of("cn-east", "cn-north"), edge.preconditions().get("region"));

        // serializes back and the instant stays ISO-8601 (not epoch numbers)
        String out = Json.mapper().writeValueAsString(req);
        assertTrue(out.contains("2026-06-01T02:00:00Z"), out);
    }

    @Test
    @DisplayName("full response serializes with steps, checkpoints and tzdb metadata")
    void responseSerialization() throws Exception {
        PlanRequest req = new PlanRequest(
                new PlanRequest.Graph(List.of("a", "b"), List.of(Edge.of("a", "b", 1, true))),
                "a", "b", Instant.parse("2026-06-01T00:00:00Z"),
                Map.of(), PlanRequest.Mode.UPGRADE, null, 1);
        PlanResponse response = new PlanningService("json-test").plan(req);
        String json = Json.mapper().writeValueAsString(response); // must not throw
        assertTrue(json.contains("\"status\":\"FOUND\""));
        assertTrue(json.contains("CP-0") && json.contains("CP-1"));
        assertTrue(json.contains("tzdbVersion"));
    }

    @Test
    @DisplayName("malformed JSON surfaces as a parse exception handled by the CLI layer")
    void malformedJson() {
        assertThrows(Exception.class,
                () -> Json.mapper().readValue("{not json", PlanRequest.class));
    }
}
