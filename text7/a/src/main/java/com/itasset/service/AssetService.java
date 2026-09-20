package com.itasset.service;

import com.itasset.domain.Asset;
import com.itasset.domain.DepreciationMethod;
import com.itasset.repo.AssetRepository;
import com.itasset.web.CreateAssetRequest;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.util.List;

@Service
public class AssetService {

    private final AssetRepository assets;

    public AssetService(AssetRepository assets) {
        this.assets = assets;
    }

    @Transactional
    public Asset create(CreateAssetRequest req) {
        if (assets.existsByAssetCode(req.assetCode())) {
            throw new ConflictException("资产编号已存在: " + req.assetCode());
        }
        ParameterAdjustmentService.validateParams(req.purchaseCost(), req.salvageValue(),
                req.usefulLifeMonths(), req.depreciationMethod(), req.decliningRatePct());
        if (req.inServiceDate() == null) {
            throw new BusinessRuleException("启用日期不能为空");
        }
        BigDecimal rate = req.depreciationMethod() == DepreciationMethod.DECLINING_BALANCE
                ? req.decliningRatePct() : null;
        Asset asset = new Asset(req.assetCode(), req.name(), req.purchaseCost(), req.salvageValue(),
                req.inServiceDate(), req.usefulLifeMonths(), req.department(),
                req.depreciationMethod(), rate);
        return assets.save(asset);
    }

    @Transactional(readOnly = true)
    public Asset get(Long id) {
        return assets.findById(id).orElseThrow(() -> new NotFoundException("资产不存在: " + id));
    }

    @Transactional(readOnly = true)
    public List<Asset> list() {
        return assets.findAll();
    }
}
