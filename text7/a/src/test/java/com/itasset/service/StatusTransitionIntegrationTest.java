package com.itasset.service;
import com.itasset.AbstractIntegrationTest;

import com.itasset.domain.Asset;
import com.itasset.domain.AssetStatus;
import com.itasset.domain.DepreciationMethod;
import com.itasset.domain.StatusChange;
import com.itasset.repo.StatusChangeRepository;
import com.itasset.web.CreateAssetRequest;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class StatusTransitionIntegrationTest extends AbstractIntegrationTest {

    static String DB = newDatabase();

    @DynamicPropertySource
    static void props(DynamicPropertyRegistry r) {
        register(r, DB);
    }

    @Autowired AssetService assets;
    @Autowired StatusTransitionService transitions;
    @Autowired StatusChangeRepository changeRepo;

    private Asset newAsset(String code, AssetStatus status) {
        var req = new CreateAssetRequest(code, "测试机-" + code, new BigDecimal("12000"),
                BigDecimal.ZERO, LocalDate.now().minusDays(30), 24, "IT部",
                DepreciationMethod.STRAIGHT_LINE, null);
        Asset a = assets.create(req);
        if (status == AssetStatus.IN_USE) {
            transitions.transition(a.getId(), AssetStatus.IN_USE, a.getVersion(),
                    UUID.randomUUID().toString(), "领用", "manager");
        }
        return assets.get(a.getId());
    }

    @Test
    void allowedTransitionsAndDisposalIsTerminal() {
        Asset a = newAsset("ST-" + UUID.randomUUID(), AssetStatus.IN_USE);
        Long v = a.getVersion();

        StatusChange repair = transitions.transition(a.getId(), AssetStatus.UNDER_REPAIR, v,
                UUID.randomUUID().toString(), "维修", "manager");
        assertThat(repair.getFromStatus()).isEqualTo(AssetStatus.IN_USE);
        Asset afterRepair = assets.get(a.getId());
        assertThat(afterRepair.getStatus()).isEqualTo(AssetStatus.UNDER_REPAIR);
        assertThat(afterRepair.getVersion()).isEqualTo(v + 1);

        transitions.transition(a.getId(), AssetStatus.IN_USE, v + 1,
                UUID.randomUUID().toString(), "修复", "manager");
        transitions.transition(a.getId(), AssetStatus.RETIRED, v + 2,
                UUID.randomUUID().toString(), "退役", "manager");
        transitions.transition(a.getId(), AssetStatus.DISPOSED, v + 3,
                UUID.randomUUID().toString(), "处置", "manager");

        // 处置后不可恢复使用：任何转出都非法
        assertThatThrownBy(() -> transitions.transition(a.getId(), AssetStatus.IN_USE, v + 4,
                UUID.randomUUID().toString(), "尝试恢复", "manager"))
                .isInstanceOf(ConflictException.class)
                .hasMessageContaining("非法状态转换");
        assertThat(assets.get(a.getId()).getStatus()).isEqualTo(AssetStatus.DISPOSED);
    }

    @Test
    void staleExpectedVersionOnlyOneSucceeds() {
        Asset a = newAsset("ST-" + UUID.randomUUID(), AssetStatus.IN_STOCK);
        Long v = a.getVersion();

        transitions.transition(a.getId(), AssetStatus.IN_USE, v,
                UUID.randomUUID().toString(), "领用", "manager");

        // 旧版本号必须失败
        assertThatThrownBy(() -> transitions.transition(a.getId(), AssetStatus.RETIRED, v,
                UUID.randomUUID().toString(), "用旧版本退役", "manager"))
                .isInstanceOf(ConflictException.class)
                .hasMessageContaining("版本冲突");
    }

    @Test
    void duplicateRequestIsIdempotent() {
        Asset a = newAsset("ST-" + UUID.randomUUID(), AssetStatus.IN_STOCK);
        String requestId = UUID.randomUUID().toString();

        StatusChange first = transitions.transition(a.getId(), AssetStatus.IN_USE,
                a.getVersion(), requestId, "领用", "manager");
        StatusChange replay = transitions.transition(a.getId(), AssetStatus.IN_USE,
                a.getVersion() + 99, requestId, "重复提交", "manager");

        assertThat(replay.getId()).isEqualTo(first.getId());
        assertThat(changeRepo.findByAssetIdOrderByIdAsc(a.getId())).hasSize(1);
        // 重放不推进版本
        assertThat(assets.get(a.getId()).getVersion()).isEqualTo(a.getVersion() + 1);
    }

    @Test
    void concurrentTransitionsExactlyOneWins() throws Exception {
        Asset a = newAsset("ST-" + UUID.randomUUID(), AssetStatus.IN_STOCK);
        Long v = a.getVersion();
        int threads = 8;
        CountDownLatch start = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        AtomicInteger successes = new AtomicInteger();
        AtomicInteger failures = new AtomicInteger();
        AtomicReference<Throwable> unexpected = new AtomicReference<>();

        for (int i = 0; i < threads; i++) {
            pool.submit(() -> {
                try {
                    start.await();
                    transitions.transition(a.getId(), AssetStatus.IN_USE, v,
                            UUID.randomUUID().toString(), "并发领用", "manager");
                    successes.incrementAndGet();
                } catch (ConflictException e) {
                    failures.incrementAndGet();
                } catch (Throwable t) {
                    unexpected.compareAndSet(null, t);
                }
            });
        }
        start.countDown();
        pool.shutdown();
        assertThat(pool.awaitTermination(30, TimeUnit.SECONDS)).isTrue();

        assertThat(unexpected.get()).isNull();
        assertThat(successes.get()).isEqualTo(1);
        assertThat(failures.get()).isEqualTo(threads - 1);
        assertThat(changeRepo.findByAssetIdOrderByIdAsc(a.getId())).hasSize(1);
        assertThat(assets.get(a.getId()).getStatus()).isEqualTo(AssetStatus.IN_USE);
    }
}
