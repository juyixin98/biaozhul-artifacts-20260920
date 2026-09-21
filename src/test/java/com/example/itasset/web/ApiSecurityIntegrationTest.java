package com.example.itasset.web;

import com.example.itasset.AbstractIntegrationTest;
import com.example.itasset.domain.DepreciationMethod;
import com.example.itasset.service.AssetLifecycleService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.core.ParameterizedTypeReference;
import org.springframework.http.*;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 真实 MySQL + HTTP：角色权限矩阵、HTTP Basic、关账/导出全链路。
 */
class ApiSecurityIntegrationTest extends AbstractIntegrationTest {

    @Autowired
    private TestRestTemplate rest;
    @Autowired
    private AssetLifecycleService lifecycle;

    private HttpHeaders auth(String user, String pass) {
        HttpHeaders h = new HttpHeaders();
        h.setBasicAuth(user, pass);
        h.setContentType(MediaType.APPLICATION_JSON);
        return h;
    }

    @Test
    void viewerIsReadOnlyAndUnauthenticatedRejected() {
        // 无认证 401
        ResponseEntity<String> noAuth = rest.getForEntity("/api/assets", String.class);
        assertThat(noAuth.getStatusCode()).isEqualTo(HttpStatus.UNAUTHORIZED);

        // 查看者可读
        HttpEntity<Void> viewer = new HttpEntity<>(auth("viewer", "Viewer#2026"));
        ResponseEntity<String> ok = rest.exchange("/api/assets", HttpMethod.GET, viewer, String.class);
        assertThat(ok.getStatusCode()).isEqualTo(HttpStatus.OK);

        // 查看者尝试建档 403
        HttpEntity<String> postViewer = new HttpEntity<>(
                "{\"assetCode\":\"X1\",\"name\":\"n\",\"department\":\"d\","
                        + "\"purchaseCost\":1,\"salvageValue\":0,\"usefulLifeMonths\":12,"
                        + "\"depreciationMethod\":\"STRAIGHT_LINE\"}",
                auth("viewer", "Viewer#2026"));
        ResponseEntity<String> denied = rest.exchange("/api/assets", HttpMethod.POST, postViewer, String.class);
        assertThat(denied.getStatusCode()).isEqualTo(HttpStatus.FORBIDDEN);

        // 错误密码 401
        HttpEntity<Void> bad = new HttpEntity<>(auth("viewer", "wrong"));
        assertThat(rest.exchange("/api/assets", HttpMethod.GET, bad, String.class).getStatusCode())
                .isEqualTo(HttpStatus.UNAUTHORIZED);
    }

    @Test
    void managerCannotClosePeriod_financeCanAndCanExport() {
        // 经理关账 -> 403
        HttpEntity<String> managerClose = new HttpEntity<>("{\"period\":202511}",
                auth("manager", "Manager#2026"));
        assertThat(rest.exchange("/api/periods/close", HttpMethod.POST, managerClose, String.class)
                .getStatusCode()).isEqualTo(HttpStatus.FORBIDDEN);

        // 财务关账 -> 201，重复关账幂等
        HttpEntity<String> financeClose = new HttpEntity<>("{\"period\":202511}",
                auth("finance", "Finance#2026"));
        ResponseEntity<String> r1 = rest.exchange("/api/periods/close", HttpMethod.POST,
                financeClose, String.class);
        assertThat(r1.getStatusCode()).isEqualTo(HttpStatus.CREATED);
        ResponseEntity<String> r2 = rest.exchange("/api/periods/close", HttpMethod.POST,
                new HttpEntity<>("{\"period\":202511}", auth("finance", "Finance#2026")),
                String.class);
        assertThat(r2.getStatusCode()).isEqualTo(HttpStatus.CREATED);

        // 财务不能做状态转换（需要经理）-> 403
        Long id = lifecycle.create(new Dtos.CreateAssetRequest(
                "T-SEC-1", "安全测试设备", "测试部",
                new BigDecimal("1000.00"), new BigDecimal("10.00"),
                LocalDate.of(2025, 11, 1), 12, DepreciationMethod.STRAIGHT_LINE), "manager").getId();
        String body = "{\"requestId\":\"sec-block-1\",\"expectedVersion\":0,\"note\":\"x\"}";
        HttpEntity<String> financeTransition = new HttpEntity<>(body, auth("finance", "Finance#2026"));
        ResponseEntity<String> blocked = rest.exchange(
                "/api/assets/" + id + "/transitions/RETIRED", HttpMethod.POST,
                financeTransition, String.class);
        assertThat(blocked.getStatusCode()).isEqualTo(HttpStatus.FORBIDDEN);

        // 导出 CSV（viewer 也可；这里用 finance 验证表头与数据行）
        // 202511 已关账，无法计提；导出仍可访问且为 CSV
        HttpEntity<Void> finance = new HttpEntity<>(auth("finance", "Finance#2026"));
        ResponseEntity<String> csv = rest.exchange("/api/export/depreciation/202511",
                HttpMethod.GET, finance, String.class);
        assertThat(csv.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(csv.getBody()).contains("期初账面价值");
    }

    @Test
    void fullHappyPathThroughHttp_postThenExport() {
        // 经理建档（库存），领用启用，财务调整前经理计提，CSV 可见
        Long id = lifecycle.create(new Dtos.CreateAssetRequest(
                "T-SEC-2", "HTTP 全链路设备", "研发部",
                new BigDecimal("6000.00"), new BigDecimal("0.00"),
                LocalDate.of(2025, 10, 1), 12, DepreciationMethod.STRAIGHT_LINE), "manager").getId();

        HttpEntity<String> post = new HttpEntity<>("{\"period\":202510}",
                auth("manager", "Manager#2026"));
        ResponseEntity<Map> resp = rest.exchange("/api/assets/depreciation/post",
                HttpMethod.POST, post, Map.class);
        assertThat(resp.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(resp.getBody()).isNotNull();

        HttpEntity<Void> manager = new HttpEntity<>(auth("manager", "Manager#2026"));
        ParameterizedTypeReference<List<Map<String, Object>>> listRef = new ParameterizedTypeReference<>() {
        };
        ResponseEntity<List<Map<String, Object>>> entries = rest.exchange(
                "/api/assets/" + id + "/depreciation/entries", HttpMethod.GET, manager, listRef);
        assertThat(entries.getBody()).hasSize(1);
        assertThat(new BigDecimal(entries.getBody().get(0).get("charge").toString()))
                .isEqualByComparingTo("500.00");

        ResponseEntity<String> csv = rest.exchange("/api/export/depreciation/202510",
                HttpMethod.GET, new HttpEntity<>(auth("viewer", "Viewer#2026")), String.class);
        assertThat(csv.getBody()).contains("T-SEC-2").contains("500.00");
    }
}
