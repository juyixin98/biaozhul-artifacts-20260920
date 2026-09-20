package com.itasset.service;
import com.itasset.AbstractIntegrationTest;

import com.itasset.domain.Asset;
import com.itasset.domain.AssetStatus;
import com.itasset.domain.DepreciationMethod;
import com.itasset.repo.DepreciationEntryRepository;
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

/**
 * 并发场景（真实 MySQL 行锁/间隙锁）：
 * 1) 同一期间重复并发计提：最多一条分录，绝不重复记账；
 * 2) 处置与计提并发：严格串行，账目一致（当月可计提，之后停提，处置不可恢复）；
 * 3) 关账与计提并发：要么计提先落账再关账，要么关账先成功、计提被拒 —— 不会出现"关账后又记账"。
 */
class ConcurrencyIntegrationTest extends AbstractIntegrationTest {

    static String DB = newDatabase();

    @DynamicPropertySource
    static void props(DynamicPropertyRegistry r) {
        register(r, DB);
    }

    @Autowired AssetService assets;
    @Autowired DepreciationService depreciation;
    @Autowired StatusTransitionService transitions;
    @Autowired PeriodCloseService closes;
    @Autowired DepreciationEntryRepository entryRepo;
    @Autowired StatusChangeRepository changeRepo;

    private Asset inUseAsset(String code, LocalDate inService) {
        Asset a = assets.create(new CreateAssetRequest(code, "并发测试机",
                new BigDecimal("1200.00"), BigDecimal.ZERO, inService,
                12, "IT部", DepreciationMethod.STRAIGHT_LINE, null));
        transitions.transition(a.getId(), AssetStatus.IN_USE, a.getVersion(),
                UUID.randomUUID().toString(), "领用", "manager");
        return assets.get(a.getId());
    }

    @Test
    void concurrentPostingSamePeriodCreatesExactlyOneEntry() throws Exception {
        Asset a = inUseAsset("CC-DEP-" + UUID.randomUUID(), LocalDate.of(2023, 12, 1));
        int threads = 6;
        CountDownLatch start = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        AtomicInteger errors = new AtomicInteger();

        for (int i = 0; i < threads; i++) {
            pool.submit(() -> {
                try {
                    start.await();
                    depreciation.runDepreciation(a.getId(), "202401", "202401",
                            UUID.randomUUID().toString());
                } catch (ConflictException e) {
                    errors.incrementAndGet(); // 唯一约束竞争落败也可接受
                } catch (Exception e) {
                    errors.incrementAndGet();
                }
            });
        }
        start.countDown();
        pool.shutdown();
        assertThat(pool.awaitTermination(30, TimeUnit.SECONDS)).isTrue();

        var entries = entryRepo.findByAssetIdAndPeriod(a.getId(), "202401");
        assertThat(entries).isPresent();
        assertThat(entryRepo.countByAssetId(a.getId())).isEqualTo(1);
    }

    @Test
    void concurrentDisposeAndPostingIsSerializableAndConsistent() throws Exception {
        // 首折旧期间 = 当前月（2026-09），处置与计提在同一月并发。
        Asset a = inUseAsset("CC-DISP-" + UUID.randomUUID(), LocalDate.of(2026, 8, 10));
        Long version = a.getVersion();
        CountDownLatch start = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(2);
        AtomicReference<Throwable> unexpected = new AtomicReference<>();

        Runnable dispose = () -> {
            try {
                start.await();
                transitions.transition(a.getId(), AssetStatus.DISPOSED, version,
                        UUID.randomUUID().toString(), "报废处置", "manager");
            } catch (Throwable t) {
                unexpected.compareAndSet(null, t);
            }
        };
        Runnable post = () -> {
            try {
                start.await();
                depreciation.runDepreciation(a.getId(), "202609", "202609",
                        UUID.randomUUID().toString());
            } catch (Throwable t) {
                unexpected.compareAndSet(null, t);
            }
        };
        pool.submit(dispose);
        pool.submit(post);
        start.countDown();
        pool.shutdown();
        assertThat(pool.awaitTermination(30, TimeUnit.SECONDS)).isTrue();
        assertThat(unexpected.get()).isNull();

        // 一致性断言：处置必然成功且终态不可恢复；当月最多一条分录。
        assertThat(assets.get(a.getId()).getStatus()).isEqualTo(AssetStatus.DISPOSED);
        assertThat(changeRepo.findByAssetIdOrderByIdAsc(a.getId())
                .stream().filter(c -> c.getToStatus() == AssetStatus.DISPOSED).count())
                .isEqualTo(1);
        assertThat(entryRepo.countByAssetId(a.getId())).isLessThanOrEqualTo(1);
        if (entryRepo.findByAssetIdAndPeriod(a.getId(), "202609").isPresent()) {
            var e = entryRepo.findByAssetIdAndPeriod(a.getId(), "202609").orElseThrow();
            assertThat(e.getOpeningBookValue().subtract(e.getDepreciationAmount()))
                    .isEqualByComparingTo(e.getClosingBookValue());
        }

        // 处置后任何状态转换都被拒绝（不可恢复使用）。
        try {
            transitions.transition(a.getId(), AssetStatus.IN_USE,
                    assets.get(a.getId()).getVersion(),
                    UUID.randomUUID().toString(), "尝试恢复", "manager");
            assertThat(false).as("处置后不应允许恢复").isTrue();
        } catch (ConflictException expected) {
            // 预期
        }
    }

    @Test
    void concurrentCloseAndPostingNeverPostsAfterClose() throws Exception {
        Asset a = inUseAsset("CC-CLOSE-" + UUID.randomUUID(), LocalDate.of(2023, 12, 1));
        CountDownLatch start = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(2);
        AtomicReference<Throwable> postError = new AtomicReference<>();

        pool.submit(() -> {
            try {
                start.await();
                depreciation.runDepreciation(a.getId(), "202401", "202401",
                        UUID.randomUUID().toString());
            } catch (ConflictException e) {
                postError.set(e); // 关账抢先：预期被拒
            } catch (Throwable t) {
                postError.set(t);
            }
        });
        pool.submit(() -> {
            try {
                start.await();
                closes.close("202401", "finance");
            } catch (Throwable t) {
                postError.compareAndSet(null, t);
            }
        });
        start.countDown();
        pool.shutdown();
        assertThat(pool.awaitTermination(30, TimeUnit.SECONDS)).isTrue();

        // 关账必成功；分录数为 0 或 1；为 0 时计提必然收到冲突错误。
        assertThat(closes.isClosed("202401")).isTrue();
        long count = entryRepo.countByAssetId(a.getId());
        assertThat(count).isLessThanOrEqualTo(1);
        if (count == 0) {
            assertThat(postError.get()).isInstanceOf(ConflictException.class);
        }
        // 关账落定后再计提必然被拒（"关账后又记账"不可能发生）。
        try {
            depreciation.runDepreciation(a.getId(), "202401", "202401",
                    UUID.randomUUID().toString());
            assertThat(entryRepo.findByAssetIdAndPeriod(a.getId(), "202401")).isPresent();
            // 若第一次已落账，则重跑只跳过、不新增
            assertThat(entryRepo.countByAssetId(a.getId())).isEqualTo(1);
        } catch (ConflictException expected) {
            assertThat(entryRepo.countByAssetId(a.getId())).isZero();
        }
    }
}
