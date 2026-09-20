package com.example.asset;

import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.security.test.context.support.WithMockUser;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/** 角色权限：财务管计提/关账/调整，资产经理管状态，查看者只读。 */
class SecurityIT extends AbstractIntegrationTest {

    @Test
    void unauthenticatedIsRejected() throws Exception {
        mvc.perform(get("/api/assets")).andExpect(status().isUnauthorized());
    }

    @Test
    @WithMockUser(username = "viewer", roles = "VIEWER")
    void viewerCanReadButNotWrite() throws Exception {
        mvc.perform(get("/api/assets")).andExpect(status().isOk());
        mvc.perform(post("/api/depreciation/run").param("period", "2031-01"))
                .andExpect(status().isForbidden());
        mvc.perform(post("/api/periods/2031-01/close")).andExpect(status().isForbidden());
    }

    @Test
    @WithMockUser(username = "manager", roles = "ASSET_MANAGER")
    void assetManagerCannotRunDepreciationOrClosePeriod() throws Exception {
        mvc.perform(post("/api/depreciation/run").param("period", "2031-02"))
                .andExpect(status().isForbidden());
        mvc.perform(post("/api/periods/2031-02/close")).andExpect(status().isForbidden());
        mvc.perform(post("/api/assets/1/adjustments")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"newCost\": 100.00, \"reason\": \"x\"}"))
                .andExpect(status().isForbidden());
    }

    @Test
    @WithMockUser(username = "finance", roles = "FINANCE")
    void financeCannotTransitionOrCreateAssets() throws Exception {
        mvc.perform(post("/api/assets/1/transitions")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"toStatus\": \"IN_USE\", \"expectedVersion\": 0, \"requestId\": \"r1\"}"))
                .andExpect(status().isForbidden());
        mvc.perform(post("/api/assets")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"assetCode\":\"SEC-1\",\"name\":\"x\",\"purchaseCost\":100.00,"
                                + "\"salvageValue\":0.00,\"commissionDate\":\"2026-01-01\","
                                + "\"usefulLifeMonths\":12,\"department\":\"d\",\"depreciationMethod\":\"STRAIGHT_LINE\"}"))
                .andExpect(status().isForbidden());
    }

    @Test
    @WithMockUser(username = "finance", roles = "FINANCE")
    void financeCanRunDepreciationAndClosePeriod() throws Exception {
        mvc.perform(post("/api/depreciation/run").param("period", "2031-03"))
                .andExpect(status().isOk());
        mvc.perform(post("/api/periods/2031-03/close")).andExpect(status().isOk());
        // 已关账期间再计提 -> 409
        mvc.perform(post("/api/depreciation/run").param("period", "2031-03"))
                .andExpect(status().isConflict());
    }
}
