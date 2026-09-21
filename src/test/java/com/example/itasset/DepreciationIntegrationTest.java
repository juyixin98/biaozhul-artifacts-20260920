package com.example.itasset;

import com.example.itasset.support.ApiClient;
import org.junit.jupiter.api.Test;

import java.math.BigDecimal;
import java.util.LinkedHashMap;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Straight-line and declining-balance posting: monthly charges, salvage floor,
 * idempotent reruns and run-to-period filling.
 *
 * <p>A month-end run is a global, once-per-period fact, so each test method uses its own
 * disjoint period window (the test clock is pinned at 2031-06-30).
 */
class DepreciationIntegrationTest extends AbstractIntegrationTest {

    private long createAndActivate(String code, String method, double cost, double salvage,
                                   String activationDate, int life) {
        Map<String, Object> asset = new LinkedHashMap<>();
        asset.put("assetCode", code);
        asset.put("name", code);
        asset.put("department", "IT");
        asset.put("cost", cost);
        asset.put("salvageValue", salvage);
        asset.put("method", method);
        long id = api.post("/api/assets", "manager", "d-create-" + code, asset).json.get("id").asLong();
        long v = api.get("/api/assets/" + id, "viewer").json.get("version").asLong();

        Map<String, Object> act = new LinkedHashMap<>();
        act.put("targetStatus", "IN_USE");
        act.put("expectedVersion", v);
        act.put("effectiveDate", activationDate);
        act.put("usefulLifeMonths", life);
        act.put("reason", "deploy");
        assertThat(api.post("/api/assets/" + id + "/transitions", "manager",
                "d-activate-" + code, act).status).isEqualTo(200);
        return id;
    }

    private ApiClient.Response run(String requestId, String period) {
        return api.post("/api/depreciation/runs", "finance", requestId, Map.of("period", period));
    }

    @Test
    void straightLine_postsConstantChargeAndEndsAtSalvage() {
        // Window 2030-02..04: activated Jan-2030, 300.00/month from 2030-02.
        long id = createAndActivate("D-SL", "SL", 12000, 1200, "2030-01-10", 36);

        String runId = "sl-run-2030-02";
        ApiClient.Response first = run(runId, "2030-02");
        assertThat(first.status).isEqualTo(200);
        assertEntry(id, "2030-02", 12000.00, 300.00, 11700.00);
        assertNbv(id, 11700);

        // Rerun same period with a NEW request id: nothing booked twice
        assertThat(run("sl-run-2030-02-again", "2030-02").status).isEqualTo(200);
        assertNbv(id, 11700);

        // Same request id: exact replay
        assertThat(run(runId, "2030-02").status).isEqualTo(200);
        assertNbv(id, 11700);

        // Running forward fills 2030-03 and 2030-04 at 300 each
        run("sl-run-2030-04", "2030-04");
        assertNbv(id, 11100);
        assertEntry(id, "2030-03", 11700.00, 300.00, 11400.00);
        assertEntry(id, "2030-04", 11400.00, 300.00, 11100.00);
    }

    @Test
    void straightLine_finalPeriodPlugsToExactSalvage() {
        // 100.00 cost, 10.00 salvage, 3 months -> raw monthly = 30.00 (90/3).
        long id = createAndActivate("D-SL-PLUG", "SL", 100, 10, "2030-05-01", 3);
        run("plug-2030-06", "2030-06");
        run("plug-2030-07", "2030-07");
        run("plug-2030-08", "2030-08");
        assertNbv(id, 10.00);
        // After the useful life, further runs post nothing for this asset
        assertThat(run("plug-2030-09", "2030-09").status).isEqualTo(200);
        assertNbv(id, 10.00);
    }

    @Test
    void roundedStraightLine_neverDropsBelowSalvage() {
        // 10.00 depreciable over 6 months -> 1.666... rounded 1.67; accumulated rounding is
        // absorbed by the final-period plug, NBV never below the 0 salvage.
        long id = createAndActivate("D-SL-ROUND", "SL", 10, 0, "2030-10-01", 6);
        for (int i = 11; i <= 12; i++) {
            run("round-2030-%02d".formatted(i), "2030-%02d".formatted(i));
        }
        for (int i = 1; i <= 4; i++) {
            run("round-2031-%02d".formatted(i), "2031-%02d".formatted(i));
        }
        assertNbv(id, 0.00);
        ApiClient.Response last = api.get("/api/depreciation/entries?period=2031-04", "viewer");
        BigDecimal lastCharge = new BigDecimal(findEntry(last, id).get("charge").asText());
        // Final charge = remainder to salvage (1.65), not another rounded 1.67
        assertThat(lastCharge).isEqualByComparingTo("1.65");
    }

    @Test
    void decliningBalance_appliesFixedRateAndSalvageFloor() {
        // 24000, salvage 2400, life 48 -> monthly rate 2/48/12 = 0.04166667
        long id = createAndActivate("D-DDB", "DDB", 24000, 2400, "2031-04-05", 48);
        run("ddb-2031-05", "2031-05");
        // 24000 * 0.04166667 = 1000.00
        assertNbv(id, 23000);
        run("ddb-2031-06", "2031-06");
        // 23000 * 0.04166667 = 958.33341 -> 958.33
        assertNbv(id, 22041.67);
    }

    @Test
    void runForFuturePeriod_isRejected() {
        // Relative to the pinned clock 2031-06-30, July is still a future period.
        assertThat(run("future", "2031-07").status).isEqualTo(422);
    }

    private void assertNbv(long id, double expected) {
        BigDecimal nbv = new BigDecimal(
                api.get("/api/assets/" + id, "viewer").json.get("nbv").asText());
        assertThat(nbv).isEqualByComparingTo(BigDecimal.valueOf(expected));
    }

    private void assertEntry(long id, String period, double opening, double charge, double closing) {
        ApiClient.Response entries = api.get("/api/depreciation/entries?period=" + period, "viewer");
        var node = findEntry(entries, id);
        assertThat(new BigDecimal(node.get("openingNbv").asText())).isEqualByComparingTo(BigDecimal.valueOf(opening));
        assertThat(new BigDecimal(node.get("charge").asText())).isEqualByComparingTo(BigDecimal.valueOf(charge));
        assertThat(new BigDecimal(node.get("closingNbv").asText())).isEqualByComparingTo(BigDecimal.valueOf(closing));
    }

    private com.fasterxml.jackson.databind.JsonNode findEntry(ApiClient.Response entries, long assetId) {
        for (var node : entries.json) {
            if (node.get("assetId").asLong() == assetId) {
                return node;
            }
        }
        throw new AssertionError("no entry for asset " + assetId);
    }
}
