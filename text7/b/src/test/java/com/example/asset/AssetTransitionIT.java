package com.example.asset;

import com.example.asset.domain.Asset;
import com.example.asset.domain.AssetStatus;
import com.example.asset.domain.DepreciationMethod;
import com.example.asset.service.ApiException;
import com.example.asset.service.AssetService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.MediaType;
import org.springframework.security.test.context.support.WithMockUser;

import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

/** 状态转换：幂等、乐观并发、状态机约束、处置终态。 */
class AssetTransitionIT extends AbstractIntegrationTest {

    @Autowired
    private AssetService assetService;

    private Map<String, Object> transitionBody(AssetStatus to, long version, String requestId) {
        return Map.of("toStatus", to.name(), "expectedVersion", version, "requestId", requestId);
    }

    @Test
    @WithMockUser(username = "manager", roles = "ASSET_MANAGER")
    void duplicateRequestIdIsIdempotent() throws Exception {
        Asset asset = newAsset("1000.00", "0.00", 12, DepreciationMethod.STRAIGHT_LINE,
                AssetStatus.IN_STOCK, "2026-01-01");
        String requestId = "req-" + UUID.randomUUID();
        String body = om.writeValueAsString(transitionBody(AssetStatus.IN_USE, 0, requestId));

        var first = mvc.perform(post("/api/assets/{id}/transitions", asset.getId())
                        .contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.replayed").value(false))
                .andReturn().getResponse().getContentAsString();
        long firstTransitionId = om.readTree(first).get("transitionId").asLong();

        // 相同 requestId 重复提交：返回首次记录，不重复转换，版本不再增加
        mvc.perform(post("/api/assets/{id}/transitions", asset.getId())
                        .contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.replayed").value(true))
                .andExpect(jsonPath("$.transitionId").value(firstTransitionId))
                .andExpect(jsonPath("$.currentVersion").value(1));

        assertThat(assetRepository.findById(asset.getId()).orElseThrow().getVersion()).isEqualTo(1);
    }

    @Test
    @WithMockUser(username = "manager", roles = "ASSET_MANAGER")
    void staleExpectedVersionIsRejected() throws Exception {
        Asset asset = newAsset("1000.00", "0.00", 12, DepreciationMethod.STRAIGHT_LINE,
                AssetStatus.IN_STOCK, "2026-01-01");
        mvc.perform(post("/api/assets/{id}/transitions", asset.getId())
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(om.writeValueAsString(transitionBody(AssetStatus.IN_USE, 7, "req-" + UUID.randomUUID()))))
                .andExpect(status().isConflict());
        assertThat(assetRepository.findById(asset.getId()).orElseThrow().getStatus())
                .isEqualTo(AssetStatus.IN_STOCK);
    }

    @Test
    @WithMockUser(username = "manager", roles = "ASSET_MANAGER")
    void illegalTransitionAndDisposedIsTerminal() throws Exception {
        Asset asset = newAsset("1000.00", "0.00", 12, DepreciationMethod.STRAIGHT_LINE,
                AssetStatus.IN_STOCK, "2026-01-01");
        // 库存不能直接处置
        mvc.perform(post("/api/assets/{id}/transitions", asset.getId())
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(om.writeValueAsString(transitionBody(AssetStatus.DISPOSED, 0, "req-" + UUID.randomUUID()))))
                .andExpect(status().isUnprocessableEntity());

        // 走完 IN_USE -> RETIRED -> DISPOSED 后，任何转换都被拒绝（处置不可恢复）
        long v = 0;
        for (AssetStatus to : List.of(AssetStatus.IN_USE, AssetStatus.RETIRED, AssetStatus.DISPOSED)) {
            mvc.perform(post("/api/assets/{id}/transitions", asset.getId())
                            .contentType(MediaType.APPLICATION_JSON)
                            .content(om.writeValueAsString(transitionBody(to, v++, "req-" + UUID.randomUUID()))))
                    .andExpect(status().isCreated());
        }
        mvc.perform(post("/api/assets/{id}/transitions", asset.getId())
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(om.writeValueAsString(transitionBody(AssetStatus.IN_USE, v, "req-" + UUID.randomUUID()))))
                .andExpect(status().isUnprocessableEntity());
        assertThat(assetRepository.findById(asset.getId()).orElseThrow().getStatus())
                .isEqualTo(AssetStatus.DISPOSED);
    }

    @Test
    void concurrentTransitionsOnlyOneSucceeds() throws Exception {
        Asset asset = newAsset("1000.00", "0.00", 12, DepreciationMethod.STRAIGHT_LINE,
                AssetStatus.IN_STOCK, "2026-01-01");

        int threads = 8;
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        CountDownLatch gate = new CountDownLatch(1);
        AtomicInteger success = new AtomicInteger();
        AtomicInteger conflict = new AtomicInteger();
        List<Future<?>> futures = new java.util.ArrayList<>();
        for (int i = 0; i < threads; i++) {
            final String requestId = "race-" + UUID.randomUUID();
            futures.add(pool.submit(() -> {
                try {
                    gate.await();
                    assetService.transition(asset.getId(), AssetStatus.IN_USE, 0, requestId, "tester");
                    success.incrementAndGet();
                } catch (ApiException.Conflict e) {
                    conflict.incrementAndGet();
                } catch (Exception e) {
                    throw new RuntimeException(e);
                }
            }));
        }
        gate.countDown();
        for (Future<?> f : futures) {
            f.get(30, TimeUnit.SECONDS);
        }
        pool.shutdown();

        assertThat(success.get()).isEqualTo(1);
        assertThat(conflict.get()).isEqualTo(threads - 1);
        Asset reloaded = assetRepository.findById(asset.getId()).orElseThrow();
        assertThat(reloaded.getStatus()).isEqualTo(AssetStatus.IN_USE);
        assertThat(reloaded.getVersion()).isEqualTo(1);
    }

    @Test
    @WithMockUser(username = "viewer", roles = "VIEWER")
    void viewerCannotTransition() throws Exception {
        Asset asset = newAsset("1000.00", "0.00", 12, DepreciationMethod.STRAIGHT_LINE,
                AssetStatus.IN_STOCK, "2026-01-01");
        mvc.perform(post("/api/assets/{id}/transitions", asset.getId())
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(om.writeValueAsString(transitionBody(AssetStatus.IN_USE, 0, "req-" + UUID.randomUUID()))))
                .andExpect(status().isForbidden());
    }
}
