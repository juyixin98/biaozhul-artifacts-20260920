package com.example.itasset.service;

import com.example.itasset.domain.*;
import com.example.itasset.repo.AssetRepository;
import com.example.itasset.repo.StatusTransitionRepository;
import com.example.itasset.web.ApiException;
import com.example.itasset.web.Dtos;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Clock;
import java.time.Instant;
import java.time.LocalDate;
import java.util.Optional;

/**
 * 资产建档与状态转换。
 *
 * <p>转换请求携带 expectedVersion（乐观锁）与 requestId（幂等）：</p>
 * <ul>
 *   <li>重复 requestId：原样返回首次结果，不重复变更；</li>
 *   <li>并发修改同一版本：JPA 乐观锁只允许一个提交成功，其余 409；</li>
 *   <li>资产当前状态与仅追加流水在同一事务提交。</li>
 * </ul>
 */
@Service
public class AssetLifecycleService {

    private final AssetRepository assets;
    private final StatusTransitionRepository transitions;
    private final DepreciationService depreciationService;
    private final Clock clock;

    public AssetLifecycleService(AssetRepository assets,
                                 StatusTransitionRepository transitions,
                                 DepreciationService depreciationService,
                                 Clock clock) {
        this.assets = assets;
        this.transitions = transitions;
        this.depreciationService = depreciationService;
        this.clock = clock;
    }

    @Transactional
    public Asset create(Dtos.CreateAssetRequest req, String operator) {
        if (assets.findByAssetCode(req.assetCode()).isPresent()) {
            throw ApiException.conflict("ASSET_CODE_EXISTS", "资产编号已存在: " + req.assetCode());
        }
        if (req.salvageValue().compareTo(req.purchaseCost()) > 0) {
            throw ApiException.unprocessable("SALVAGE_TOO_HIGH",
                    "残值 " + req.salvageValue() + " 不得高于采购成本 " + req.purchaseCost());
        }
        // 启用日期必须是过去或当月（不允许未来启用）
        if (req.placedInService() != null
                && req.placedInService().isAfter(LocalDate.now(clock))) {
            throw ApiException.unprocessable("FUTURE_PLACEMENT", "启用日期不得晚于当前日期");
        }
        Asset asset = new Asset(req.assetCode(), req.name(), req.department(),
                req.purchaseCost(), req.salvageValue(), req.placedInService(),
                req.usefulLifeMonths(), req.depreciationMethod(), Instant.now(clock));
        if (req.placedInService() != null) {
            // 直接登记为已启用资产：进入“使用中”并建立初始政策
            asset.setStatus(AssetStatus.IN_USE);
            asset = assets.save(asset);
            depreciationService.createInitialPolicy(asset, "建档时直接启用", operator);
            appendTransition(asset, "bootstrap-" + asset.getId(), null, AssetStatus.IN_USE,
                    0L, "建档时直接启用", operator);
        }
        return assets.save(asset);
    }

    /**
     * 执行状态转换。返回转换后的资产。重复 requestId 返回已存在转换对应的资产快照。
     */
    @Transactional
    public Asset transition(Long assetId, AssetStatus target, Dtos.TransitionRequest req, String operator) {
        Optional<StatusTransition> prior = transitions.findByRequestId(req.requestId());
        if (prior.isPresent()) {
            StatusTransition t = prior.get();
            if (!t.getAssetId().equals(assetId) || t.getToStatus() != target) {
                throw ApiException.conflict("REQUEST_ID_CONFLICT",
                        "requestId 已用于另一笔转换: " + req.requestId());
            }
            return assets.findById(t.getAssetId())
                    .orElseThrow(() -> ApiException.notFound("资产不存在: id=" + assetId));
        }

        Asset asset = assets.findById(assetId)
                .orElseThrow(() -> ApiException.notFound("资产不存在: id=" + assetId));

        if (!asset.getVersion().equals(req.expectedVersion())) {
            throw ApiException.conflict("VERSION_MISMATCH",
                    "预期版本 " + req.expectedVersion() + " 与当前版本 " + asset.getVersion() + " 不一致");
        }
        AssetStatus from = asset.getStatus();
        if (!from.canTransitionTo(target)) {
            throw ApiException.unprocessable("ILLEGAL_TRANSITION",
                    "不允许从状态 " + from + " 转换到 " + target
                            + "；处置后资产不可恢复使用");
        }

        applySideEffects(asset, target, operator);
        asset.setStatus(target);
        assets.save(asset); // @Version 冲突 -> ObjectOptimisticLockingFailureException -> 409

        appendTransition(asset, req.requestId(), from, target,
                req.expectedVersion(), req.note(), operator);

        // 极端并发：两个不同 requestId 同时提交，唯一约束兜底
        try {
            transitions.flush();
        } catch (DataIntegrityViolationException e) {
            throw ApiException.conflict("REQUEST_ID_CONFLICT",
                    "requestId 已存在: " + req.requestId());
        }
        return asset;
    }

    private void appendTransition(Asset asset, String requestId, AssetStatus from, AssetStatus to,
                                  long expectedVersion, String note, String operator) {
        transitions.save(new StatusTransition(
                asset.getId(), requestId, from, to, expectedVersion, note,
                operator, Instant.now(clock)));
    }

    /**
     * 转换副作用：库存 -> 使用中时记录启用日期并建立折旧政策。
     * 退役、处置不改账：计提仅对 IN_USE / IN_REPAIR 开放，退役当月之后不再计提，
     * 处置为终态；已计提分录原样保留。退役/处置与计提的并发一致性由
     * 计提路径上的悲观行锁 + 状态检查保证（见 DepreciationService）。
     */
    private void applySideEffects(Asset asset, AssetStatus target, String operator) {
        if (target == AssetStatus.IN_USE && asset.getStatus() == AssetStatus.IN_STOCK) {
            if (asset.getPlacedInService() == null) {
                LocalDate today = LocalDate.now(clock);
                asset.setPlacedInService(today);
                assets.save(asset);
                depreciationService.createInitialPolicy(asset, "库存领用启用", operator);
            }
        }
    }

    // ------------------------------------------------------------------
    // 查询
    // ------------------------------------------------------------------

    @Transactional(readOnly = true)
    public Asset requireAsset(Long id) {
        return assets.findById(id)
                .orElseThrow(() -> ApiException.notFound("资产不存在: id=" + id));
    }

    @Transactional(readOnly = true)
    public java.util.List<Asset> listAssets() {
        return assets.findAll();
    }
}
