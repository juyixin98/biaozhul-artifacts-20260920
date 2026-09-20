package com.itasset.web;
import com.itasset.AbstractIntegrationTest;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.http.MediaType;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.web.servlet.MockMvc;

import java.util.UUID;

import static org.springframework.security.test.web.servlet.request.SecurityMockMvcRequestPostProcessors.httpBasic;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/** 三角色 HTTP Basic 权限：财务可关账/调整，经理可维护资产与状态，查看者只读。 */
@AutoConfigureMockMvc
class SecurityRolesIntegrationTest extends AbstractIntegrationTest {

    static String DB = newDatabase();

    @DynamicPropertySource
    static void props(DynamicPropertyRegistry r) {
        register(r, DB);
    }

    @Autowired MockMvc mvc;

    private String newAssetJson() {
        return """
            {"assetCode":"SEC-%s","name":"权限测试机","purchaseCost":1000.00,
             "salvageValue":0,"inServiceDate":"2024-01-01","usefulLifeMonths":12,
             "department":"IT部","depreciationMethod":"STRAIGHT_LINE"}
            """.formatted(UUID.randomUUID().toString().substring(0, 8));
    }

    @Test
    void viewerIsReadOnly() throws Exception {
        mvc.perform(get("/api/assets").with(httpBasic("viewer", "viewer123")))
                .andExpect(status().isOk());
        mvc.perform(post("/api/assets").with(httpBasic("viewer", "viewer123"))
                        .contentType(MediaType.APPLICATION_JSON).content(newAssetJson()))
                .andExpect(status().isForbidden());
    }

    @Test
    void managerManagesAssetsButCannotClosePeriods() throws Exception {
        mvc.perform(post("/api/assets").with(httpBasic("manager", "manager123"))
                        .contentType(MediaType.APPLICATION_JSON).content(newAssetJson()))
                .andExpect(status().isCreated());
        mvc.perform(post("/api/periods/202401/close")
                        .with(httpBasic("manager", "manager123")))
                .andExpect(status().isForbidden());
    }

    @Test
    void financeClosesPeriodsButCannotCreateAssets() throws Exception {
        mvc.perform(post("/api/periods/202401/close")
                        .with(httpBasic("finance", "finance123")))
                .andExpect(status().isCreated());
        mvc.perform(post("/api/assets").with(httpBasic("finance", "finance123"))
                        .contentType(MediaType.APPLICATION_JSON).content(newAssetJson()))
                .andExpect(status().isForbidden());
    }

    @Test
    void wrongAndMissingCredentialsRejected() throws Exception {
        mvc.perform(get("/api/assets").with(httpBasic("viewer", "bad")))
                .andExpect(status().isUnauthorized());
        mvc.perform(get("/api/assets"))
                .andExpect(status().isUnauthorized());
    }
}
