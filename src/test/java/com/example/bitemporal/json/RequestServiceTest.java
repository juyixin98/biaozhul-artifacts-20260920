package com.example.bitemporal.json;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

class RequestServiceTest {

    private ObjectMapper mapper;
    private RequestService service;

    @BeforeEach
    void setUp() {
        mapper = new ObjectMapper()
                .registerModule(new JavaTimeModule())
                .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS);
        service = new RequestService();
    }

    private JsonNode run(String json) {
        try {
            return mapper.valueToTree(service.handle(mapper.readTree(json)));
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    private JsonNode runExpectError(String json) {
        try {
            JsonNode out = mapper.valueToTree(service.handle(mapper.readTree(json)));
            assertTrue(out.get("ok").asBoolean(), "expected error envelope");
            return null;
        } catch (com.example.bitemporal.engine.BitemporalException e) {
            return mapper.valueToTree(java.util.Map.of("message", e.getMessage()));
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    @Test
    void infoReportsTzdbVersion() {
        JsonNode out = run("{\"op\":\"info\"}");
        assertTrue(out.get("ok").asBoolean());
        JsonNode tz = out.get("timezone");
        assertNotNull(tz.get("tzdbVersion").asText());
        assertFalse(tz.get("tzdbVersion").asText().isBlank());
        assertTrue(tz.get("tzdbVersion").asText().matches("\\d{4}[a-z]")
                        || tz.get("tzdbVersion").asText().equals("unknown"),
                "TZDB 版本形如 2024a，实际: " + tz.get("tzdbVersion").asText());
    }

    @Test
    void asOfBaseline() {
        JsonNode out = run("""
                {"op":"asOf","entityId":"E001",
                 "businessDate":"2025-07-01","observationDate":"2026-01-15"}
                """);
        assertEquals("TechLead", out.get("record").get("role").asText());
    }

    @Test
    void asOfBeforeSeedReturnsNull() {
        JsonNode out = run("""
                {"op":"asOf","entityId":"E001",
                 "businessDate":"2025-07-01","observationDate":"2025-12-31"}
                """);
        assertTrue(out.get("record").isNull());
    }

    @Test
    void commitOverlapRejected() {
        JsonNode err = runExpectError("""
                {"op":"commit","transactionDate":"2026-02-01","changes":[
                  {"entityId":"E001","department":"X","role":"Y",
                   "validFrom":"2025-06-01","validTo":"2025-08-01","mode":"INSERT"}
                ]}
                """);
        assertNotNull(err);
        assertTrue(err.get("message").asText().contains("overlap"));
    }

    @Test
    void correctionScenarioShowsBothWorlds() {
        JsonNode out = run("""
                {
                  "op": "scenario",
                  "steps": [
                    {"type":"asOf","entityId":"E001",
                     "businessDate":"2025-05-01","observationDate":"2026-01-15"},
                    {"type":"commit","transactionDate":"2026-02-15","changes":[
                      {"entityId":"E001","department":"Platform","role":"SRE",
                       "validFrom":"2025-03-01","validTo":"2025-09-01","mode":"CORRECTION"}
                    ]},
                    {"type":"asOf","entityId":"E001",
                     "businessDate":"2025-05-01","observationDate":"2026-01-15"},
                    {"type":"asOf","entityId":"E001",
                     "businessDate":"2025-05-01","observationDate":"2026-06-01"}
                  ]
                }
                """);
        JsonNode steps = out.get("steps");
        // 修订前观察：Dev；历史可重现：仍是 Dev；修订后观察：SRE
        assertEquals("Dev", steps.get(0).get("record").get("role").asText());
        assertEquals("Dev", steps.get(2).get("record").get("role").asText());
        assertEquals("SRE", steps.get(3).get("record").get("role").asText());
        assertEquals("Platform", steps.get(3).get("record").get("department").asText());
    }

    @Test
    void unknownOpRejected() {
        JsonNode err = runExpectError("{\"op\":\"nope\"}");
        assertNotNull(err);
    }

    @Test
    void badDateRejected() {
        JsonNode err = runExpectError("""
                {"op":"asOf","entityId":"E001",
                 "businessDate":"2025/07/01","observationDate":"2026-01-01"}
                """);
        assertNotNull(err);
        assertTrue(err.get("message").asText().contains("ISO date"));
    }

    @Test
    void missingOpRejected() {
        JsonNode err = runExpectError("{}");
        assertNotNull(err);
    }

    @Test
    void commitSuccessThenSnapshotListsSeedAndLog() {
        JsonNode out = run("""
                {"op":"commit","transactionDate":"2026-02-15","changes":[
                  {"entityId":"E001","department":"Platform","role":"SRE",
                   "validFrom":"2025-03-01","validTo":"2025-09-01","mode":"CORRECTION"}
                ]}
                """);
        assertTrue(out.get("ok").asBoolean());
        assertEquals(8, out.get("physicalRowCount").asInt());

        JsonNode snap = run("{\"op\":\"snapshot\"}");
        assertEquals(5, snap.get("rows").size(), "snapshot 基于全新装载数据，只有种子 5 行");
        assertEquals(1, snap.get("commitLog").size());
        assertEquals(5, snap.get("commitLog").get(0).get("changeCount").asInt());
    }

    @Test
    void historyReturnsTimeline() {
        JsonNode out = run("""
                {"op":"history","entityId":"E002","observationDate":"2026-01-15"}
                """);
        assertEquals(2, out.get("history").size());
        assertEquals("Rep", out.get("history").get(0).get("role").asText());
        assertEquals("Manager", out.get("history").get(1).get("role").asText());
    }

    @Test
    void asOfAllReturnsThreeEntities() {
        JsonNode out = run("""
                {"op":"asOfAll","businessDate":"2025-11-01","observationDate":"2026-01-15"}
                """);
        assertEquals(3, out.get("records").size());
    }

    @Test
    void scenarioRejectsUnknownStepType() {
        JsonNode err = runExpectError("""
                {"op":"scenario","steps":[{"type":"bogus"}]}
                """);
        assertNotNull(err);
        assertTrue(err.get("message").asText().contains("step type"));
    }

    @Test
    void invalidModeRejected() {
        JsonNode err = runExpectError("""
                {"op":"commit","transactionDate":"2026-02-01","changes":[
                  {"entityId":"E009","department":"Ops","role":"SRE",
                   "validFrom":"2025-01-01","mode":"UPSERT"}
                ]}
                """);
        assertNotNull(err);
        assertTrue(err.get("message").asText().contains("INSERT or CORRECTION"));
    }
}
