package com.example.drvb.server;

import com.example.drvb.core.RuleRegistry;
import com.example.drvb.stream.InMemoryResultStore;
import com.example.drvb.stream.RuleBindingEngine;
import com.example.drvb.stream.WatermarkTracker;
import com.example.drvb.time.ManualScheduler;
import com.example.drvb.time.SimClock;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Black-box test driving the real HTTP service over loopback, with
 * SimClock + ManualScheduler so timelines are deterministic.
 */
class HttpServerMainTest {

    private static final ObjectMapper M = JsonCodecs.MAPPER;

    private HttpServerMain app;
    private String base;
    private final HttpClient http = HttpClient.newHttpClient();

    @BeforeEach
    void startServer() throws Exception {
        SimClock clock = new SimClock(1_000L);
        RuleBindingEngine engine = new RuleBindingEngine(
                new RuleRegistry(), new WatermarkTracker(0),
                new InMemoryResultStore(), clock, 2_000_000L, 10_000_000L);
        app = new HttpServerMain(engine, clock, new ManualScheduler(), true);
        app.start(0, 60_000L);
        base = "http://localhost:" + app.port();
    }

    @AfterEach
    void stopServer() {
        app.stop();
    }

    private HttpResponse<String> post(String path, JsonNode body) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(M.writeValueAsString(body)))
                .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path)).GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private JsonNode json(HttpResponse<String> r) throws Exception {
        return M.readTree(r.body());
    }

    private ObjectNode rule(String id, int threshold) {
        ObjectNode rule = M.createObjectNode();
        rule.put("id", id);
        rule.put("name", "big-" + threshold);
        rule.put("eventType", "payment");
        rule.put("action", "BLOCK");
        ObjectNode cond = rule.putObject("condition");
        cond.put("op", "gte");
        cond.put("field", "amount");
        cond.put("value", threshold);
        return rule;
    }

    private ObjectNode version(String id, Long effectiveFrom, ObjectNode... rules) {
        ObjectNode v = M.createObjectNode();
        v.put("versionId", id);
        if (effectiveFrom != null) {
            v.put("effectiveFrom", effectiveFrom);
        }
        ArrayNode rs = v.putArray("rules");
        for (ObjectNode r : rules) {
            rs.add(r);
        }
        return v;
    }

    private ObjectNode event(String id, long timeSec, int amount) {
        ObjectNode e = M.createObjectNode();
        e.put("id", id);
        e.put("type", "payment");
        e.put("eventTime", timeSec * 1000L);
        e.putObject("data").put("amount", amount);
        return e;
    }

    @Test
    void fullLifecycleOverHttp() throws Exception {
        // health
        HttpResponse<String> health = get("/health");
        assertEquals(200, health.statusCode());
        assertTrue(json(health).get("simMode").asBoolean());

        // event before bootstrap -> rejected MISSING_VERSION
        HttpResponse<String> early = post("/events", event("e0", 500, 999));
        assertEquals(200, early.statusCode());
        JsonNode earlyBody = json(early);
        assertEquals(0, earlyBody.get("acceptedCount").asInt());
        assertEquals("MISSING_VERSION",
                earlyBody.get("results").get(0).get("reason").asText());

        // bootstrap v1
        HttpResponse<String> b = post("/admin/bootstrap",
                version("v1", null, rule("big", 100)));
        assertEquals(201, b.statusCode());
        String checksum1 = json(b).get("version").get("checksum").asText();
        assertEquals(64, checksum1.length());

        // double bootstrap -> 409
        assertEquals(409, post("/admin/bootstrap",
                version("v1b", null, rule("big", 100))).statusCode());

        // publish v2 (effective 1000) and v3 (effective 2000) in advance
        assertEquals(201, post("/admin/rules/versions",
                version("v2", 1_000_000L, rule("big", 200))).statusCode());
        // overlapping interval -> 422
        HttpResponse<String> bad = post("/admin/rules/versions",
                version("vBad", 1_000_000L, rule("big", 250)));
        assertEquals(422, bad.statusCode());
        assertEquals("EFFECTIVE_TIME_IN_PAST", json(bad).get("error").asText());
        assertEquals(201, post("/admin/rules/versions",
                version("v3", 2_000_000L, rule("big", 300))).statusCode());

        // batch: on-time ahead event + late events interleaved
        ArrayNode batch = M.createArrayNode();
        batch.add(event("ahead", 2_500, 350)); // v3 match
        batch.add(event("late1500", 1_500, 250)); // late -> v2 match
        batch.add(event("late500", 500, 150)); // late -> v1 match
        batch.add(event("none", 1_200, 150)); // v2 no match
        ObjectNode batchReq = M.createObjectNode();
        batchReq.set("events", batch);
        HttpResponse<String> br = post("/events/batch", batchReq);
        assertEquals(200, br.statusCode());
        JsonNode results = json(br).get("results");
        assertEquals(4, json(br).get("acceptedCount").asInt(),
                "accepted even when no rule matches; rejectedCount covers only rejections");
        assertEquals(0, json(br).get("rejectedCount").asInt());
        assertEquals(List.of("v3", "v2", "v1", "v2"),
                results.findValuesAsText("versionId"));
        JsonNode late1500 = results.get(1);
        assertTrue(late1500.get("late").asBoolean());
        assertEquals("BLOCK", late1500.get("matches").get(0).get("action").asText());

        // rollback to v1 content from t=3000
        ObjectNode rb = M.createObjectNode();
        rb.put("sourceVersionId", "v1");
        rb.put("newVersionId", "rb1");
        rb.put("effectiveFrom", 3_000_000L);
        rb.put("description", "restore v1 threshold");
        HttpResponse<String> rbr = post("/admin/rules/rollback", rb);
        assertEquals(201, rbr.statusCode());
        assertTrue(json(rbr).get("checksumsMatch").asBoolean());

        // unknown rollback source -> 404
        ObjectNode rbBad = rb.deepCopy();
        rbBad.put("sourceVersionId", "ghost");
        rbBad.put("newVersionId", "rbX");
        assertEquals(404, post("/admin/rules/rollback", rbBad).statusCode());

        // event after rollback uses restored threshold
        HttpResponse<String> after = post("/events", event("after", 3_000, 150));
        JsonNode ar = json(after).get("results").get(0);
        assertEquals("rb1", ar.get("versionId").asText());
        assertEquals(1, ar.get("matches").size());

        // queries: results filtered by event time; rejected list
        HttpResponse<String> q = get("/results?from=1000000&to=2000000");
        JsonNode qb = json(q);
        assertEquals(2, qb.get("count").asInt(), "v2 match (late1500) + v2 no-match (none)");
        HttpResponse<String> rejected = get("/results/rejected");
        JsonNode rej = json(rejected);
        assertEquals(1, rej.get("count").asInt());
        assertEquals("MISSING_VERSION",
                rej.get("results").get(0).get("reason").asText());

        // GC preconditions: before purge, nothing reclaimed; after clock moves
        // past retention and a tick fires, v1 is collected; ancient event is
        // then VERSION_RECLAIMED.
        HttpResponse<String> preview = get("/admin/gc-preview");
        JsonNode pv = json(preview);
        assertTrue(pv.get("referencedVersionIds").isArray());
        List<String> eligibleIds = pv.get("eligibleForReclaim")
                .findValuesAsText("versionId");
        assertFalse(eligibleIds.contains("v1"),
                "v1 is still referenced by retained results, so it must not be "
                        + "eligible yet: " + eligibleIds);

        post("/admin/clock", M.createObjectNode().put("set", 20_000_000L));
        HttpResponse<String> tick = post("/admin/tick", M.createObjectNode());
        assertEquals(200, tick.statusCode());
        HttpResponse<String> versions = get("/admin/rules/versions");
        List<String> ids = json(versions).findValuesAsText("versionId");
        assertFalse(ids.contains("v1"), "v1 reclaimed after purge: " + ids);
        assertTrue(ids.contains("v2"),
                "v2 survives: as the new oldest version its interval may still "
                        + "receive late events up to the horizon: " + ids);
        assertTrue(ids.contains("v3"));
        assertTrue(ids.contains("rb1"));

        HttpResponse<String> ancient = post("/events", event("ancient", 100, 150));
        assertEquals("VERSION_RECLAIMED",
                json(ancient).get("results").get(0).get("reason").asText());

        // stats reflect everything
        JsonNode stats = json(get("/admin/stats"));
        assertTrue(stats.get("accepted").asInt() >= 5);
        assertEquals(2, stats.get("rejected").get("total").asInt());
        assertEquals(1, stats.get("rejected").get("MISSING_VERSION").asInt());
        assertEquals(1, stats.get("rejected").get("VERSION_RECLAIMED").asInt());
    }

    @Test
    void malformedBodiesAre400AndUnknownRoutes404() throws Exception {
        HttpResponse<String> malformed = http.send(
                HttpRequest.newBuilder(URI.create(base + "/events"))
                        .header("Content-Type", "application/json")
                        .POST(HttpRequest.BodyPublishers.ofString("{not json"))
                        .build(),
                HttpResponse.BodyHandlers.ofString());
        assertEquals(400, malformed.statusCode());
        assertEquals("MALFORMED_JSON", json(malformed).get("error").asText());

        assertEquals(404, get("/nope").statusCode());

        // valid JSON, missing required field -> BAD_REQUEST
        HttpResponse<String> missing = post("/events",
                M.createObjectNode().put("type", "payment").put("eventTime", 1L));
        assertEquals(400, missing.statusCode());
    }
}
