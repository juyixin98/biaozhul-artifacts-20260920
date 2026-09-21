package com.example.itasset;

import com.example.itasset.support.ApiClient;
import org.junit.jupiter.api.Test;

import java.math.BigDecimal;
import java.util.LinkedHashMap;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Cost / life / method adjustments preserve old parameters and a reason, affect only
 * open periods, and cannot touch closed months. Each test uses a disjoint period window
 * (test clock pinned at 2031-06-30).
 */
class AdjustmentIntegrationTest extends AbstractIntegrationTest {

    private long slAsset(String code, String inServiceDate) {
        Map<String, Object> asset = new LinkedHashMap<>();
        asset.put("assetCode", code);
        asset.put("name", code);
        asset.put("department", "R&D");
        asset.put("cost", 12000);
        asset.put("salvageValue", 1200);
        asset.put("method", "SL");
        long id = api.post("/api/assets", "manager", "adj-create-" + code, asset).json.get("id").asLong();
        long v = api.get("/api/assets/" + id, "viewer").json.get("version").asLong();
        Map<String, Object> act = new LinkedHashMap<>();
        act.put("targetStatus", "IN_USE");
        act.put("expectedVersion", v);
        act.put("effectiveDate", inServiceDate);
        act.put("usefulLifeMonths", 36);
        act.put("reason", "deploy");
        assertThat(api.post("/api/assets/" + id + "/transitions", "manager",
                "adj-act-" + code, act).status).isEqualTo(200);
        return id;
    }

    private ApiClient.Response run(String rid, String period) {
        return api.post("/api/depreciation/runs", "finance", rid, Map.of("period", period));
    }

    private ApiClient.Response adjust(long id, String rid, Map<String, Object> body) {
        return api.post("/api/assets/" + id + "/adjustments", "finance", rid, body);
    }

    private Map<String, Object> adjustment(double cost, double salvage, int life,
                                           String method, String effective, String reason) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("newCost", cost);
        body.put("newSalvageValue", salvage);
        body.put("newUsefulLifeMonths", life);
        body.put("newMethod", method);
        body.put("effectivePeriod", effective);
        body.put("reason", reason);
        return body;
    }

    @Test
    void adjustment_appliesNewRegimeProspectivelyFromNextMonth() {
        // Activated 2030-01 -> first eligible 2030-02. Book 02..04 (300 each) -> NBV 11100.
        long id = slAsset("ADJ-1", "2030-01-10");
        run("adj1-run-2030-04", "2030-04");
        assertNbv(id, 11100);

        // Effective 2030-05 (next unposted month): base NBV 11100, fresh 18m life,
        // salvage 1100 -> (11100-1100)/18 = 555.56/month.
        ApiClient.Response resp = adjust(id, "adj1-change",
                adjustment(12000, 1100, 18, "SL", "2030-05", "revised residual life and salvage"));
        assertThat(resp.status).isEqualTo(200);
        assertThat(resp.json.get("openEntriesDeleted").asInt()).isZero();
        assertThat(resp.json.get("oldSalvageValue").asDouble()).isEqualTo(1200);
        assertThat(resp.json.get("newSalvageValue").asDouble()).isEqualTo(1100);
        assertThat(resp.json.get("reason").asText()).contains("revised residual");
        assertThat(new BigDecimal(resp.json.get("newSlMonthly").asText()))
                .isEqualByComparingTo("555.56");

        run("adj1-run-2030-05", "2030-05");
        assertNbv(id, 10544.44);
    }

    @Test
    void adjustment_reversesAlreadyBookedOpenEntries() {
        // Activated 2030-06 -> first eligible 2030-07. Run through 2030-08 (books 07,08),
        // then book 2030-09 via its own run.
        long id = slAsset("ADJ-2", "2030-06-01");
        run("adj2-run-2030-08", "2030-08");
        run("adj2-run-2030-09", "2030-09");
        assertNbv(id, 11100);

        // Effective 2030-09 (the last-run month): the 09 entry is reversed, 07/08 stay.
        ApiClient.Response resp = adjust(id, "adj2-change",
                adjustment(12000, 1200, 48, "SL", "2030-09", "life correction"));
        assertThat(resp.status).isEqualTo(200);
        assertThat(resp.json.get("openEntriesDeleted").asInt()).isEqualTo(1);
        assertNbv(id, 11400); // back to after 08
        ApiClient.Response entries = api.get("/api/depreciation/entries?period=2030-09", "viewer");
        assertThat(entries.json.toString()).doesNotContain("\"assetId\":" + id);

        // The reversed 2030-09 entry must be rebookable, even though the period run row
        // exists: the run is only a request marker, entries are keyed by (asset, period).
        // Reposting happens by re-running under a new request id against 2030-09.
        ApiClient.Response rerun = run("adj2-rerun-2030-09", "2030-09");
        assertThat(rerun.status).isEqualTo(200);
        // New regime: (11400-1200)/48 = 212.50
        assertNbv(id, 11187.50);
    }

    @Test
    void adjustment_cannotAffectClosedPeriods_andRejectsGap() {
        // Seed closed 2025-12; an effective period at/before it is locked.
        long id = slAsset("ADJ-3", "2030-09-01");
        ApiClient.Response closed = adjust(id, "adj3-closed",
                adjustment(12000, 1200, 36, "SL", "2025-12", "late correction"));
        assertThat(closed.status).isEqualTo(409);

        // Effective beyond the next unposted month leaves a gap -> 422
        run("adj3-run-2030-12", "2030-12"); // books 2030-10,11,12; next unposted 2031-01
        ApiClient.Response gap = adjust(id, "adj3-gap",
                adjustment(12000, 1200, 36, "SL", "2031-03", "gap"));
        assertThat(gap.status).isEqualTo(422);
    }

    @Test
    void adjustment_isIdempotent_andOnlyFinanceCanAdjust() {
        long id = slAsset("ADJ-4", "2031-01-05"); // first eligible 2031-02
        run("adj4-run-2031-02", "2031-02");
        Map<String, Object> body = adjustment(12000, 1000, 30, "DDB", "2031-03", "switch method");
        ApiClient.Response one = adjust(id, "adj4-dup", body);
        assertThat(one.status).isEqualTo(200);
        ApiClient.Response two = adjust(id, "adj4-dup", body);
        assertThat(two.status).isEqualTo(200);
        assertThat(two.json.get("id").asLong()).isEqualTo(one.json.get("id").asLong());

        // Asset manager may not adjust
        assertThat(api.post("/api/assets/" + id + "/adjustments", "manager",
                "adj4-manager", body).status).isEqualTo(403);
    }

    @Test
    void adjustment_history_isRetained() {
        // first eligible 2031-02; adjust at 2031-02, post it, then adjust at 2031-03.
        long id = slAsset("ADJ-5", "2031-01-10");
        adjust(id, "adj5-a", adjustment(12000, 1000, 36, "SL", "2031-02", "first"));
        run("adj5-run-2031-02", "2031-02");
        adjust(id, "adj5-b", adjustment(12000, 900, 24, "SL", "2031-03", "second"));

        ApiClient.Response history = api.get("/api/assets/" + id + "/adjustments", "viewer");
        assertThat(history.status).isEqualTo(200);
        assertThat(history.json).hasSize(2);
        assertThat(history.json.get(0).get("reason").asText()).isEqualTo("second");
        assertThat(history.json.get(1).get("reason").asText()).isEqualTo("first");
    }

    private void assertNbv(long id, double expected) {
        ApiClient.Response asset = api.get("/api/assets/" + id, "viewer");
        BigDecimal nbv = new BigDecimal(asset.json.get("nbv").asText());
        assertThat(nbv).isEqualByComparingTo(BigDecimal.valueOf(expected));
    }
}
