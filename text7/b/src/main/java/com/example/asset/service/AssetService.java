package com.example.asset.service;

import com.example.asset.domain.Asset;
import com.example.asset.domain.AssetStatus;
import com.example.asset.domain.AssetStatusTransition;
import com.example.asset.repository.AssetRepository;
import com.example.asset.repository.AssetStatusTransitionRepository;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 资产状态机服务。
 * 幂等与并发语义：
 * <ul>
 *   <li>requestId 唯一：同一请求重复提交返回首次的变更记录，不重复转换；</li>
 *   <li>expectedVersion 乐观并发控制：与当前版本不一致返回 409，并发修改只有一个成功；</li>
 *   <li>资产行悲观写锁 + 同事务写入仅追加变更记录：当前状态与历史记录要么一起提交，要么一起回滚。</li>
 * </ul>
 */
@Service
public class AssetService {

    private final AssetRepository assetRepository;
    private final AssetStatusTransitionRepository transitionRepository;

    public AssetService(AssetRepository assetRepository,
                        AssetStatusTransitionRepository transitionRepository) {
        this.assetRepository = assetRepository;
        this.transitionRepository = transitionRepository;
    }

    public record TransitionResult(AssetStatusTransition transition, long currentVersion, boolean replayed) {
    }

    @Transactional
    public Asset create(Asset asset) {
        if (assetRepository.existsByAssetCode(asset.getAssetCode())) {
            throw new ApiException.Conflict("asset code already exists: " + asset.getAssetCode());
        }
        validateAmounts(asset.getPurchaseCost().compareTo(asset.getSalvageValue()) >= 0,
                "salvage value must not exceed purchase cost");
        validateAmounts(asset.getSalvageValue().signum() >= 0, "salvage value must not be negative");
        validateAmounts(asset.getUsefulLifeMonths() >= 1 && asset.getUsefulLifeMonths() <= 600,
                "useful life must be between 1 and 600 months");
        return assetRepository.save(asset);
    }

    @Transactional
    public TransitionResult transition(Long assetId, AssetStatus toStatus, long expectedVersion,
                                       String requestId, String actor) {
        // 先取行锁再做幂等检查：同一资产的并发转换在此串行化，
        // 重试请求在拿到锁后能看到首次请求已提交的变更记录。
        Asset asset = assetRepository.findWithLockById(assetId)
                .orElseThrow(() -> new ApiException.NotFound("asset not found: " + assetId));

        var existing = transitionRepository.findByRequestId(requestId);
        if (existing.isPresent()) {
            return new TransitionResult(existing.get(), asset.getVersion(), true);
        }

        if (asset.getVersion() != expectedVersion) {
            throw new ApiException.Conflict("version mismatch: expected " + expectedVersion
                    + " but current is " + asset.getVersion());
        }

        AssetStatus from = asset.getStatus();
        if (!from.canTransitionTo(toStatus)) {
            throw new ApiException.InvalidTransition("transition " + from + " -> " + toStatus + " is not allowed");
        }

        asset.setStatus(toStatus);
        AssetStatusTransition record = transitionRepository.save(
                new AssetStatusTransition(assetId, from, toStatus, requestId, expectedVersion, actor));
        assetRepository.save(asset);
        return new TransitionResult(record, asset.getVersion() + 1, false);
    }

    @Transactional(readOnly = true)
    public Asset get(Long assetId) {
        return assetRepository.findById(assetId)
                .orElseThrow(() -> new ApiException.NotFound("asset not found: " + assetId));
    }

    private static void validateAmounts(boolean condition, String message) {
        if (!condition) {
            throw new ApiException.BadRequest(message);
        }
    }
}
