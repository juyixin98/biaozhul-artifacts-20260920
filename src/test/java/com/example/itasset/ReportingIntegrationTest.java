package com.example.itasset;

import com.example.itasset.support.ApiClient;
import org.junit.jupiter.api.Test;

import java.util.LinkedHashMap;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/** Reporting: per-period CSV export and the opening/charge/closing explanation. */
class ReportingIntegrationTest extends AbstractIntegrationTest {

    private long activateSl(String code, String date, int life) {
        Map<String, Object> asset = new LinkedHashMap<>();
        asset.put("assetCode", code);
        asset.put("name", code);
        asset.put("department", "Finance");
        asset.put("cost", 12000);
        asset.put("salvageValue", 1200);
        asset.put("method", "SL");
        long id = api.post("/api/assets", "manager", "rep-create-" + code, asset).json.get("id").asLong();
        long v = api.get("/api/assets/" + id, "viewer").json.get("version").asLong();
        Map<String, Object> act = new LinkedHashMap<>();
        act.put("targetStatus", "IN_USE");
        act.put("expectedVersion", v);
        act.put("effectiveDate", date);
        act.put("usefulLifeMonths", life);
        act.put("reason", "deploy");
        assertThat(api.post("/api/assets/" + id + "/transitions", "manager",
                "rep-act-" + code, act).status).isEqualTo(200);
        return id;
    }

    @Test
    void explanation_explainsOpeningChargeClosing_andExportContainsRows() {
        long id = activateSl("RPT-1", "2031-01-12", 36);
        assertThat(api.post("/api/depreciation/runs", "finance", "rep-run-2031-03",
                Map.of("period", "2031-03")).status).isEqualTo(200);

        // Explanation: booked 02 and 03, then projected forward to the end of life.
        ApiClient.Response expl = api.get(
                "/api/assets/" + id + "/depreciation?throughPeriod=2031-05", "viewer");
        assertThat(expl.status).isEqualTo(200);
        var lines = expl.json.get("lines");
        assertThat(lines).hasSize(4); // 02,03 booked + 04,05 projected
        assertThat(lines.get(0).get("period").asText()).isEqualTo("2031-02");
        assertThat(lines.get(0).get("openingNbv").asDouble()).isEqualTo(12000.0);
        assertThat(lines.get(0).get("charge").asDouble()).isEqualTo(300.0);
        assertThat(lines.get(0).get("closingNbv").asDouble()).isEqualTo(11700.0);
        assertThat(lines.get(0).get("booked").asBoolean()).isTrue();
        assertThat(lines.get(3).get("booked").asBoolean()).isFalse();
        assertThat(lines.get(3).get("charge").asDouble()).isEqualTo(300.0);
        // Opening of each line equals closing of the previous one
        for (int i = 1; i < lines.size(); i++) {
            assertThat(lines.get(i).get("openingNbv").asText())
                    .isEqualTo(lines.get(i - 1).get("closingNbv").asText());
        }

        // CSV export for 2031-03 carries the asset row and a total
        ApiClient.Response csv = api.get("/api/export/depreciation?period=2031-03", "viewer");
        assertThat(csv.status).isEqualTo(200);
        assertThat(csv.body).contains("period,assetId,assetCode");
        assertThat(csv.body).contains("RPT-1");
        assertThat(csv.body).contains("2031-03");
        assertThat(csv.body.lines()).anyMatch(l -> l.startsWith("2031-03,,,,,,,,"));
    }
}
