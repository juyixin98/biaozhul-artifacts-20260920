package com.example.itasset;

import com.example.itasset.support.ApiClient;
import org.junit.jupiter.api.Test;

import java.util.LinkedHashMap;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/** Closed periods are immutable: no posting and no adjustment into them. */
class PeriodCloseIntegrationTest extends AbstractIntegrationTest {

    private ApiClient.Response closeAs(String user, String requestId, String period) {
        return api.post("/api/periods/close", user, requestId, Map.of("period", period));
    }

    private ApiClient.Response run(String requestId, String period) {
        return api.post("/api/depreciation/runs", "finance", requestId, Map.of("period", period));
    }

    @Test
    void cannotPostDepreciationIntoClosedPeriod() {
        // Seed already has 2025-12 closed.
        ApiClient.Response list = api.get("/api/periods", "viewer");
        assertThat(list.status).isEqualTo(200);
        assertThat(list.json.toString()).contains("2025-12");

        ApiClient.Response postClosed = run("pc-post-closed", "2025-12");
        assertThat(postClosed.status).isEqualTo(409);
        assertThat(postClosed.json.get("message").asText()).contains("closed");

        // Idempotent close of the same request id
        ApiClient.Response c1 = closeAs("finance", "pc-close-2026-01", "2026-01");
        assertThat(c1.status).isEqualTo(200);
        ApiClient.Response c1again = closeAs("finance", "pc-close-2026-01", "2026-01");
        assertThat(c1again.status).isEqualTo(200);

        // Cannot skip ahead: 2026-03 rejected while 2026-02 is open
        ApiClient.Response gap = closeAs("finance", "pc-skip", "2026-03");
        assertThat(gap.status).isEqualTo(422);

        // Once 2026-01 is closed, posting for it is refused
        ApiClient.Response postJan = run("pc-post-jan", "2026-01");
        assertThat(postJan.status).isEqualTo(409);
    }

    @Test
    void onlyFinanceCanClose() {
        assertThat(closeAs("manager", "pc-close-manager", "2026-02").status).isEqualTo(403);
        assertThat(closeAs("viewer", "pc-close-viewer", "2026-02").status).isEqualTo(403);
        // Finance's ability to close is asserted positively in cannotPostDepreciationIntoClosedPeriod.
    }

    @Test
    void viewerCannotMutateAnything() {
        Map<String, Object> asset = new LinkedHashMap<>();
        asset.put("assetCode", "PC-V");
        asset.put("name", "v");
        asset.put("department", "IT");
        asset.put("cost", 100);
        asset.put("salvageValue", 10);
        asset.put("method", "SL");
        assertThat(api.post("/api/assets", "viewer", "pc-viewer-create", asset).status).isEqualTo(403);
        // reads are fine
        assertThat(api.get("/api/assets", "viewer").status).isEqualTo(200);
    }
}
