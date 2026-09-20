package com.example.asset.service;

import com.example.asset.domain.Asset;
import com.example.asset.domain.AssetAdjustment;
import com.example.asset.domain.AssetStatus;
import com.example.asset.repository.AssetAdjustmentRepository;
import com.example.asset.repository.AssetRepository;
import com.example.asset.repository.DepreciationEntryRepository;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.math.RoundingMode;

/**
 * 参数调整服务（财务角色）。
 * 未来适用法：成本/使用年限调整只影响未关账期间的后续计提；
 * 已计提的期间（无论是否关账）一律不回溯修改。每次调整保存原因与旧参数，可追溯。
 */
@Service
public class AdjustmentService {

    private final AssetRepository assetRepository;
    private final AssetAdjustmentRepository adjustmentRepository;
    private final DepreciationEntryRepository entryRepository;

    public AdjustmentService(AssetRepository assetRepository,
                             AssetAdjustmentRepository adjustmentRepository,
                             DepreciationEntryRepository entryRepository) {
        this.assetRepository = assetRepository;
        this.adjustmentRepository = adjustmentRepository;
        this.entryRepository = entryRepository;
    }

    @Transactional
    public AssetAdjustment adjust(Long assetId, BigDecimal newCost, Integer newUsefulLifeMonths,
                                  String reason, String actor) {
        Asset asset = assetRepository.findWithLockById(assetId)
                .orElseThrow(() -> new ApiException.NotFound("asset not found: " + assetId));

        if (asset.getStatus() == AssetStatus.DISPOSED) {
            throw new ApiException.BadRequest("disposed asset cannot be adjusted");
        }
        BigDecimal cost = newCost != null ? newCost : asset.getPurchaseCost();
        int life = newUsefulLifeMonths != null ? newUsefulLifeMonths : asset.getUsefulLifeMonths();

        if (cost.signum() <= 0) {
            throw new ApiException.BadRequest("cost must be positive");
        }
        if (cost.compareTo(asset.getSalvageValue()) < 0) {
            throw new ApiException.BadRequest("cost must not be lower than salvage value");
        }
        int elapsed = entryRepository.countByAssetId(assetId);
        if (life <= elapsed) {
            throw new ApiException.BadRequest(
                    "new useful life (" + life + ") must exceed already depreciated months (" + elapsed + ")");
        }
        if (cost.equals(asset.getPurchaseCost()) && life == asset.getUsefulLifeMonths()) {
            throw new ApiException.BadRequest("nothing to adjust: parameters are unchanged");
        }

        // 成本差额并入账面价值（未来适用）。尚未开始计提的资产账面价值即成本本身，
        // 直接以新成本为准，不再叠加差额，避免重复计算。
        BigDecimal delta = BigDecimal.ZERO.setScale(2, RoundingMode.HALF_UP);
        if (elapsed > 0) {
            delta = cost.subtract(asset.getPurchaseCost()).setScale(2, RoundingMode.HALF_UP);
            asset.setPendingBookValueDelta(asset.getPendingBookValueDelta().add(delta));
        }

        AssetAdjustment adjustment = adjustmentRepository.save(new AssetAdjustment(
                assetId, asset.getPurchaseCost(), cost,
                asset.getUsefulLifeMonths(), life, delta, reason, actor));

        asset.setPurchaseCost(cost);
        asset.setUsefulLifeMonths(life);
        assetRepository.save(asset);
        return adjustment;
    }
}
