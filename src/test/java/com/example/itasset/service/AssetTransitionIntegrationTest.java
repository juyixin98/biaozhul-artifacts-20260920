package com.example.itasset.service;

import com.example.itasset.AbstractIntegrationTest;
import com.example.itasset.domain.Asset;
import com.example.itasset.domain.AssetStatus;
import com.example.itasset.domain.DepreciationMethod;
import com.example.itasset.repo.StatusTransitionRepository;
import com.example.itasset.web.ApiException;
import com.example.itasset.web.Dtos;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * 真实 MySQL：状态机、幂等、乐观锁并发（只有一个成功）、处置终态、同事务流水。
 */
class AssetTransitionIntegrationTest extends AbstractIntegrationTest {

    @Autowired
    private AssetLifecycleService lifecycle;
    @Autowired
    private StatusTransitionRepository transitionRepo;

    private Asset createInStock(String code) {
        return lifecycle.create(new Dtos.CreateAssetRequest(
                code, "交换机-" + code, "网络部",
                new BigDecimal("5000.00"), new BigDecimal("200.00"),
                null, 36, DepreciationMethod.STRAIGHT_LINE), "manager");
    }

    @Test
    void allowedTransitionsAndDisposalIsTerminal() {
        Asset a = createInStock("T-TR-1");
        Long id = a.getId();
        long v = a.getVersion();

        Asset inUse = lifecycle.transition(id, AssetStatus.IN_USE,
                new Dtos.TransitionRequest(UUID.randomUUID().toString(), v, "领用"), "manager");
        assertThat(inUse.getStatus()).isEqualTo(AssetStatus.IN_USE);
        assertThat(inUse.getVersion()).isEqualTo(v + 1);
        assertThat(inUse.getPlacedInService()).isNotNull();

        Asset repair = lifecycle.transition(id, AssetStatus.IN_REPAIR,
                new Dtos.TransitionRequest(UUID.randomUUID().toString(), inUse.getVersion(), null), "manager");
        Asset back = lifecycle.transition(id, AssetStatus.IN_USE,
                new Dtos.TransitionRequest(UUID.randomUUID().toString(), repair.getVersion(), null), "manager");
        Asset retired = lifecycle.transition(id, AssetStatus.RETIRED,
                new Dtos.TransitionRequest(UUID.randomUUID().toString(), back.getVersion(), "到期退役"), "manager");
        Asset disposed = lifecycle.transition(id, AssetStatus.DISPOSED,
                new Dtos.TransitionRequest(UUID.randomUUID().toString(), retired.getVersion(), "回收处置"), "manager");

        assertThat(disposed.getStatus()).isEqualTo(AssetStatus.DISPOSED);

        // 处置后不可恢复使用
        assertThatThrownBy(() -> lifecycle.transition(id, AssetStatus.IN_USE,
                new Dtos.TransitionRequest(UUID.randomUUID().toString(), disposed.getVersion(), "尝试恢复"),
                "manager"))
                .isInstanceOfSatisfying(ApiException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo("ILLEGAL_TRANSITION"));

        // 退役也不能直接回使用中
        assertThatThrownBy(() -> lifecycle.transition(id, AssetStatus.IN_USE,
                new Dtos.TransitionRequest(UUID.randomUUID().toString(), retired.getVersion(), null),
                "manager"))
                .isInstanceOf(ApiException.class);
    }

    @Test
    void illegalDirectTransitionRejected() {
        Asset a = createInStock("T-TR-2");
        // 库存不能直接退役
        assertThatThrownBy(() -> lifecycle.transition(a.getId(), AssetStatus.RETIRED,
                new Dtos.TransitionRequest(UUID.randomUUID().toString(), a.getVersion(), null), "manager"))
                .isInstanceOfSatisfying(ApiException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo("ILLEGAL_TRANSITION"));
        // 状态未改变、流水未增加
        assertThat(transitionRepo.findByAssetIdOrderByIdAsc(a.getId())).isEmpty();
    }

    @Test
    void duplicateRequestIsIdempotent() {
        Asset a = createInStock("T-TR-3");
        String requestId = UUID.randomUUID().toString();
        Dtos.TransitionRequest req =
                new Dtos.TransitionRequest(requestId, a.getVersion(), "幂等测试");

        Asset first = lifecycle.transition(a.getId(), AssetStatus.IN_USE, req, "manager");
        Asset second = lifecycle.transition(a.getId(), AssetStatus.IN_USE, req, "manager");

        assertThat(second.getVersion()).isEqualTo(first.getVersion());
        assertThat(transitionRepo.findByAssetIdOrderByIdAsc(a.getId())).hasSize(1);
    }

    @Test
    void sameRequestIdForDifferentMoveConflicts() {
        Asset a = createInStock("T-TR-4");
        String requestId = UUID.randomUUID().toString();
        lifecycle.transition(a.getId(), AssetStatus.IN_USE,
                new Dtos.TransitionRequest(requestId, a.getVersion(), null), "manager");

        // 同 requestId 指向不同目标转换 -> 409
        Asset fresh = lifecycle.requireAsset(a.getId());
        assertThatThrownBy(() -> lifecycle.transition(a.getId(), AssetStatus.RETIRED,
                new Dtos.TransitionRequest(requestId, fresh.getVersion(), null), "manager"))
                .isInstanceOfSatisfying(ApiException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo("REQUEST_ID_CONFLICT"));
    }

    @Test
    void staleVersionRejectedBeforeStateChange() {
        Asset a = createInStock("T-TR-5");
        long staleVersion = a.getVersion();
        // 先成功一次
        lifecycle.transition(a.getId(), AssetStatus.IN_USE,
                new Dtos.TransitionRequest(UUID.randomUUID().toString(), staleVersion, null), "manager");
        // 用旧版本再转 -> 409
        assertThatThrownBy(() -> lifecycle.transition(a.getId(), AssetStatus.IN_REPAIR,
                new Dtos.TransitionRequest(UUID.randomUUID().toString(), staleVersion, null), "manager"))
                .isInstanceOfSatisfying(ApiException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo("VERSION_MISMATCH"));
    }

    @Test
    void concurrentTransitionsOnlyOneSucceeds() throws Exception {
        Asset a = createInStock("T-TR-6");
        long version = a.getVersion();

        final Asset[] winners = new Asset[2];
        final Throwable[] errors = new Throwable[2];
        Thread t1 = new Thread(() -> {
            try {
                winners[0] = lifecycle.transition(a.getId(), AssetStatus.IN_USE,
                        new Dtos.TransitionRequest(UUID.randomUUID().toString(), version, null), "manager");
            } catch (Throwable e) {
                errors[0] = unwrap(e);
            }
        });
        Thread t2 = new Thread(() -> {
            try {
                winners[1] = lifecycle.transition(a.getId(), AssetStatus.IN_USE,
                        new Dtos.TransitionRequest(UUID.randomUUID().toString(), version, null), "manager");
            } catch (Throwable e) {
                errors[1] = unwrap(e);
            }
        });
        t1.start();
        t2.start();
        t1.join();
        t2.join();

        int successCount = (winners[0] != null ? 1 : 0) + (winners[1] != null ? 1 : 0);
        assertThat(successCount).as("同一版本并发转换只能一个成功").isEqualTo(1);
        assertThat(errors[0] == null || errors[1] == null).isTrue();
        Throwable loser = errors[0] != null ? errors[0] : errors[1];
        // 失败方：显式版本校验冲突、提交时乐观锁失败，或悲观锁/死锁并发冲突，均属“只允许一个成功”
        assertThat(loser).satisfiesAnyOf(
                t -> assertThat(t).isInstanceOf(ApiException.class),
                t -> assertThat(t).isInstanceOf(org.springframework.dao.ConcurrencyFailureException.class));

        // 流水恰好一条（状态与流水同事务）
        assertThat(transitionRepo.findByAssetIdOrderByIdAsc(a.getId())).hasSize(1);
    }

    private static Throwable unwrap(Throwable t) {
        // 事务代理抛出的异常保持原样即可；仅用于日志可读
        return t;
    }
}
