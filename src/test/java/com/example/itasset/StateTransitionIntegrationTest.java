package com.example.itasset;

import com.example.itasset.support.ApiClient;
import org.junit.jupiter.api.Test;

import java.util.LinkedHashMap;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Whitelisted transitions, append-only audit, expected-version optimistic concurrency,
 * idempotent retries, and the unrecoverability of DISPOSED.
 */
class StateTransitionIntegrationTest extends AbstractIntegrationTest {


    private ApiClient.Response createAsset(String code) {
        Map<String, Object> req = new LinkedHashMap<>();
        req.put("assetCode", code);
        req.put("name", "Laptop " + code);
        req.put("department", "IT");
        req.put("cost", 12000.00);
        req.put("salvageValue", 1200.00);
        req.put("method", "SL");
        return api.post("/api/assets", "manager", "create-" + code, req);
    }

    private Map<String, Object> activateBody(long version, String date, int life) {
        Map<String, Object> req = new LinkedHashMap<>();
        req.put("targetStatus", "IN_USE");
        req.put("expectedVersion", version);
        req.put("effectiveDate", date);
        req.put("usefulLifeMonths", life);
        req.put("reason", "deploy");
        return req;
    }

    private Map<String, Object> transitionBody(long version, String target, String reason) {
        Map<String, Object> req = new LinkedHashMap<>();
        req.put("targetStatus", target);
        req.put("expectedVersion", version);
        req.put("reason", reason);
        return req;
    }

    @Test
    void fullLifecycle_isAuditedAndDisposalIsTerminal() {
        long id = createAsset("T-001").json.get("id").asLong();
        long v0 = getVersion(id);

        // IN_STOCK -> IN_USE (activate)
        ApiClient.Response activated = api.post("/api/assets/" + id + "/transitions", "manager",
                "t1-activate", activateBody(v0, "2026-01-15", 36));
        assertThat(activated.status).isEqualTo(200);
        assertThat(activated.json.get("toStatus").asText()).isEqualTo("IN_USE");
        long v1 = activated.json.get("asset").get("version").asLong();
        assertThat(v1).isEqualTo(v0 + 1);

        // IN_USE -> UNDER_REPAIR
        ApiClient.Response repair = api.post("/api/assets/" + id + "/transitions", "manager",
                "t1-repair", transitionBody(v1, "UNDER_REPAIR", "broken fan"));
        assertThat(repair.status).isEqualTo(200);
        long v2 = repair.json.get("asset").get("version").asLong();

        // UNDER_REPAIR -> IN_USE
        ApiClient.Response back = api.post("/api/assets/" + id + "/transitions", "manager",
                "t1-back", transitionBody(v2, "IN_USE", "fixed"));
        assertThat(back.status).isEqualTo(200);
        long v3 = back.json.get("asset").get("version").asLong();

        // IN_USE -> RETIRED
        ApiClient.Response retire = api.post("/api/assets/" + id + "/transitions", "manager",
                "t1-retire", transitionBody(v3, "RETIRED", "lease end"));
        assertThat(retire.status).isEqualTo(200);
        long v4 = retire.json.get("asset").get("version").asLong();

        // RETIRED cannot come back: RETIRED -> IN_USE forbidden
        ApiClient.Response revive = api.post("/api/assets/" + id + "/transitions", "manager",
                "t1-revive", transitionBody(v4, "IN_USE", "oops"));
        assertThat(revive.status).isEqualTo(422);

        // RETIRED -> DISPOSED
        ApiClient.Response dispose = api.post("/api/assets/" + id + "/transitions", "manager",
                "t1-dispose", transitionBody(v4, "DISPOSED", "sold"));
        assertThat(dispose.status).isEqualTo(200);
        long v5 = dispose.json.get("asset").get("version").asLong();

        // DISPOSED is terminal: every move rejected
        ApiClient.Response afterDispose = api.post("/api/assets/" + id + "/transitions", "manager",
                "t1-after", transitionBody(v5, "IN_USE", "recover"));
        assertThat(afterDispose.status).isEqualTo(422);
        assertThat(getStatus(id)).isEqualTo("DISPOSED");

        // Append-only trail contains all five transitions in order
        ApiClient.Response history = api.get("/api/assets/" + id + "/transitions", "viewer");
        assertThat(history.status).isEqualTo(200);
        assertThat(history.json.isArray()).isTrue();
        assertThat(history.json).hasSize(5);
        assertThat(history.json.get(0).get("toStatus").asText()).isEqualTo("IN_USE");
        assertThat(history.json.get(4).get("toStatus").asText()).isEqualTo("DISPOSED");
    }

    @Test
    void illegalTransition_isRejected() {
        long id = createAsset("T-002").json.get("id").asLong();
        long v = getVersion(id);
        // IN_STOCK -> UNDER_REPAIR is not allowed
        ApiClient.Response resp = api.post("/api/assets/" + id + "/transitions", "manager",
                "t2-bad", transitionBody(v, "UNDER_REPAIR", "nope"));
        assertThat(resp.status).isEqualTo(422);
        assertThat(getStatus(id)).isEqualTo("IN_STOCK");
    }

    @Test
    void staleExpectedVersion_onlyOneWriterSucceeds() {
        long id = createAsset("T-003").json.get("id").asLong();
        long v0 = getVersion(id);

        ApiClient.Response first = api.post("/api/assets/" + id + "/transitions", "manager",
                "t3-first", activateBody(v0, "2026-02-10", 24));
        assertThat(first.status).isEqualTo(200);

        // Retry with the same stale version 0 but a NEW request id -> 409
        ApiClient.Response concurrent = api.post("/api/assets/" + id + "/transitions", "manager",
                "t3-lostrace", activateBody(v0, "2026-02-10", 24));
        assertThat(concurrent.status).isEqualTo(409);
        assertThat(concurrent.json.get("message").asText()).contains("Version mismatch");

        // Only one transition was recorded
        ApiClient.Response history = api.get("/api/assets/" + id + "/transitions", "viewer");
        assertThat(history.json).hasSize(1);
    }

    @Test
    void duplicateRequestId_isIdempotent_andSamePayloadReplays() {
        long id = createAsset("T-004").json.get("id").asLong();
        long v = getVersion(id);

        ApiClient.Response one = api.post("/api/assets/" + id + "/transitions", "manager",
                "t4-dup", activateBody(v, "2026-03-01", 36));
        assertThat(one.status).isEqualTo(200);
        long transitionId = one.json.get("id").asLong();

        // Same request id + same body -> identical transition record, no new row
        ApiClient.Response two = api.post("/api/assets/" + id + "/transitions", "manager",
                "t4-dup", activateBody(v, "2026-03-01", 36));
        assertThat(two.status).isEqualTo(200);
        assertThat(two.json.get("id").asLong()).isEqualTo(transitionId);

        ApiClient.Response history = api.get("/api/assets/" + id + "/transitions", "viewer");
        assertThat(history.json).hasSize(1);

        // Same request id with a DIFFERENT body -> 409
        ApiClient.Response wrong = api.post("/api/assets/" + id + "/transitions", "manager",
                "t4-dup", activateBody(v, "2026-04-01", 48));
        assertThat(wrong.status).isEqualTo(409);
    }

    @Test
    void missingRequestId_isRejected() {
        Map<String, Object> req = new LinkedHashMap<>();
        req.put("assetCode", "T-005");
        req.put("name", "NoReq");
        req.put("department", "IT");
        req.put("cost", 100);
        req.put("salvageValue", 10);
        req.put("method", "SL");
        ApiClient.Response resp = api.exchange(
                org.springframework.http.HttpMethod.POST, "/api/assets", "manager", null, req);
        assertThat(resp.status).isEqualTo(400);
    }

    private long getVersion(long id) {
        return api.get("/api/assets/" + id, "viewer").json.get("version").asLong();
    }

    private String getStatus(long id) {
        return api.get("/api/assets/" + id, "viewer").json.get("status").asText();
    }
}
