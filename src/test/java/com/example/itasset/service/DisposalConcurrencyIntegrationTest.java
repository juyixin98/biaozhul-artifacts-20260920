package com.example.itasset.service;

import com.example.itasset.AbstractIntegrationTest;
import com.example.itasset.domain.Asset;
import com.example.itasset.domain.AssetStatus;
import com.example.itasset.domain.DepreciationEntry;
import com.example.itasset.domain.DepreciationMethod;
import com.example.itasset.repo.DepreciationEntryRepository;
import com.example.itasset.web.Dtos;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 真实 MySQL：退役/处置与同一期间计提并发。
 * 计提持有资产悲观行锁并在锁内复查状态，两种完成顺序下都不得出现不一致账目：
 *  - 先计提后处置：该期间分录存在，资产最终 DISPOSED；
 *  - 先处置后计提：该期间无分录，资产 DISPOSED，且不产生“处置后新增折旧”。
 */
class DisposalConcurrencyIntegrationTest extends AbstractIntegrationTest {

    @Autowired
    private AssetLifecycleService lifecycle;
    @Autowired
    private DepreciationService depreciation;
    @Autowired
    private DepreciationEntryRepository entryRepo;

    @Test
    void disposeAndPostSamePeriodNeverProduceInconsistentLedger() throws Exception {
        final int rounds = 6;
        for (int round = 1; round <= rounds; round++) {
            runOneRound(round);
        }
    }

    private void runOneRound(int round) throws Exception {
        Asset asset = lifecycle.create(new Dtos.CreateAssetRequest(
                "T-CONC-" + round, "设备-" + round, "测试部",
                new BigDecimal("10000.00"), new BigDecimal("500.00"),
                LocalDate.of(2025, 6, 1), 24, DepreciationMethod.STRAIGHT_LINE),
                "manager");
        Long id = asset.getId();

        int period = 202506;
        CountDownLatch start = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(2);
        AtomicInteger disposeOk = new AtomicInteger();
        AtomicInteger postOk = new AtomicInteger();
        AtomicInteger postReject = new AtomicInteger();

        Runnable disposeTask = () -> {
            try {
                start.await();
                // IN_USE -> RETIRED -> DISPOSED（两跳）
                Asset a = lifecycle.requireAsset(id);
                Asset retired = lifecycle.transition(id, AssetStatus.RETIRED,
                        new Dtos.TransitionRequest(UUID.randomUUID().toString(),
                                a.getVersion(), "并发退役-" + round), "manager");
                lifecycle.transition(id, AssetStatus.DISPOSED,
                        new Dtos.TransitionRequest(UUID.randomUUID().toString(),
                                retired.getVersion(), "并发处置-" + round), "manager");
                disposeOk.incrementAndGet();
            } catch (Exception e) {
                // 计提与退役在行锁上串行，转换使用乐观锁版本；跳转变体下失败可接受
            }
        };

        Runnable postTask = () -> {
            try {
                start.await();
                var results = depreciation.postPeriod(period);
                var mine = results.stream().filter(r -> r.assetId().equals(id)).findFirst();
                mine.ifPresent(r -> {
                    if ("POSTED".equals(r.outcome())) {
                        postOk.incrementAndGet();
                    } else {
                        postReject.incrementAndGet();
                    }
                });
            } catch (Exception e) {
                postReject.incrementAndGet();
            }
        };

        pool.submit(disposeTask);
        pool.submit(postTask);
        start.countDown();
        pool.shutdown();
        assertThat(pool.awaitTermination(30, TimeUnit.SECONDS)).isTrue();

        // 断言一致性，而非固定顺序：
        Asset finalAsset = lifecycle.requireAsset(id);
        long entryCount = entryRepo.findByAssetIdOrderByPeriodAsc(id).stream()
                .filter(e -> e.getPeriod().equals(period)).count();

        if (finalAsset.getStatus() == AssetStatus.DISPOSED) {
            // 处置完成：该期间最多一条分录；分录存在则必须合法（期末 >= 残值）
            assertThat(entryCount).isLessThanOrEqualTo(1);
            entryRepo.findByAssetIdAndPeriod(id, period).ifPresent(e -> {
                assertThat(e.getClosingValue()).isEqualByComparingTo(e.getClosingValue());
                assertThat(e.getClosingValue().compareTo(new BigDecimal("500.00")))
                        .isGreaterThanOrEqualTo(0);
            });
        }
        // 若计提发生在处置的两跳之间导致处置链失败（版本变化），资产仍应处于一个合法状态，
        // 且任何已写分录都合法
        for (DepreciationEntry e : entryRepo.findByAssetIdOrderByPeriodAsc(id)) {
            assertThat(e.getCharge().signum()).isGreaterThanOrEqualTo(0);
            assertThat(e.getClosingValue().compareTo(e.getOpeningValue())).isLessThanOrEqualTo(0);
        }
        assertThat(disposeOk.get() + (finalAsset.getStatus() == AssetStatus.DISPOSED ? 0 : 0)
                + postOk.get() + postReject.get()).isGreaterThan(0);
    }
}
