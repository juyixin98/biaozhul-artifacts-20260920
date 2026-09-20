package com.itasset.service;

import com.itasset.domain.Asset;
import com.itasset.domain.AssetStatus;
import com.itasset.domain.DepreciationEntry;
import com.itasset.domain.ParameterAdjustment;
import com.itasset.repo.AssetRepository;
import com.itasset.repo.DepreciationEntryRepository;
import com.itasset.repo.ParameterAdjustmentRepository;
import com.itasset.repo.PeriodCloseRepository;
import com.itasset.repo.PeriodMutexRepository;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.util.List;

/**
 * 折旧参数调整（成本/残值/使用年限/方法/比率），FINANCE 角色。
 *
 * <h3>可追溯与影响范围</h3>
 * <ul>
 *   <li>调整只能从<b>下一个尚未计提的期间</b>生效（未来适用法）：
 *       已生成的仅追加分录（尤其已关账期间）永不改写；</li>
 *   <li>生效期间必须未关账：先持 period_mutex 再查关账标记，
 *       与计提/关账事务串行；</li>
 *   <li>每次调整保存原因、旧参数、新参数、调整时点账面价值与新旧段月数；</li>
 *   <li>调整开启新折旧段：以调整时点账面价值为基数，在
 *       （新使用月数 - 已计提月数）内摊销。</li>
 * </ul>
 */
@Service
public class ParameterAdjustmentService {

    private final AssetRepository assets;
    private final DepreciationEntryRepository entries;
    private final ParameterAdjustmentRepository adjustments;
    private final PeriodCloseRepository closes;
    private final PeriodMutexRepository mutexes;

    public ParameterAdjustmentService(AssetRepository assets, DepreciationEntryRepository entries,
                                      ParameterAdjustmentRepository adjustments,
                                      PeriodCloseRepository closes,
                                      PeriodMutexRepository mutexes) {
        this.assets = assets;
        this.entries = entries;
        this.adjustments = adjustments;
        this.closes = closes;
        this.mutexes = mutexes;
    }

    public record AdjustmentCommand(
            String effectivePeriod,
            BigDecimal newPurchaseCost,
            BigDecimal newSalvageValue,
            Integer newUsefulLifeMonths,
            com.itasset.domain.DepreciationMethod newMethod,
            BigDecimal newDecliningRatePct,
            String reason,
            String requestId) {
    }

    @Transactional
    public ParameterAdjustment adjust(Long assetId, AdjustmentCommand cmd) {
        if (cmd.requestId() == null || cmd.requestId().isBlank()) {
            throw new BusinessRuleException("requestId 不能为空");
        }
        if (cmd.reason() == null || cmd.reason().isBlank()) {
            throw new BusinessRuleException("调整原因不能为空");
        }

        Asset asset = assets.findByIdForUpdate(assetId)
                .orElseThrow(() -> new NotFoundException("资产不存在: " + assetId));

        ParameterAdjustment prior = adjustments.findByRequestId(cmd.requestId()).orElse(null);
        if (prior != null) {
            if (!prior.getAssetId().equals(assetId)) {
                throw new ConflictException("requestId 已用于其他调整请求");
            }
            return prior;
        }

        if (asset.getStatus() == AssetStatus.DISPOSED
                || asset.getStatus() == AssetStatus.RETIRED) {
            throw new ConflictException("资产已退役/处置，不可再调整折旧参数");
        }

        String effective = cmd.effectivePeriod();
        if (effective == null || !effective.matches("\\d{6}")) {
            throw new BusinessRuleException("生效期间格式必须为 yyyyMM");
        }

        // 期间互斥 + 关账检查（锁序：asset -> period，与折旧一致，避免死锁）。
        mutexes.insertIgnore(effective);
        mutexes.lock(effective).orElseThrow(() -> new IllegalStateException("period_mutex 缺失"));
        if (closes.isPeriodFrozen(effective)) {
            throw new ConflictException("生效期间 " + effective + " 已关账，调整只能影响未关账期间");
        }

        List<DepreciationEntry> posted = entries.findByAssetIdOrderByPeriodAsc(assetId);
        String firstPeriod = Periods.firstDepreciationPeriod(asset.getInServiceDate());
        String expectedNext = posted.isEmpty() ? firstPeriod
                : Periods.plus(posted.get(posted.size() - 1).getPeriod(), 1);
        if (!effective.equals(expectedNext)) {
            throw new ConflictException("调整只能从下一个未计提期间 " + expectedNext
                    + " 起生效（请求生效期间 " + effective + "）；已计提期间不可改写");
        }

        // 新参数取值与校验（缺省即沿用现值）。
        BigDecimal newCost = nvl(cmd.newPurchaseCost(), asset.getPurchaseCost());
        BigDecimal newSalvage = nvl(cmd.newSalvageValue(), asset.getSalvageValue());
        int newLife = cmd.newUsefulLifeMonths() == null
                ? asset.getUsefulLifeMonths() : cmd.newUsefulLifeMonths();
        var newMethod = cmd.newMethod() == null ? asset.getDepreciationMethod() : cmd.newMethod();
        // 方法切换时必须给出新比率；沿用余额递减法且未传比率时沿用旧比率。
        BigDecimal newRate;
        if (newMethod == com.itasset.domain.DepreciationMethod.DECLINING_BALANCE) {
            newRate = cmd.newDecliningRatePct() != null
                    ? cmd.newDecliningRatePct() : asset.getDecliningRatePct();
        } else {
            newRate = null;
        }

        validateParams(newCost, newSalvage, newLife, newMethod, newRate);

        int elapsed = posted.size();
        if (newLife <= elapsed) {
            throw new ConflictException("新使用月数 " + newLife
                    + " 必须大于已计提月数 " + elapsed);
        }
        BigDecimal bookValue = posted.isEmpty() ? asset.getPurchaseCost()
                : posted.get(posted.size() - 1).getClosingBookValue();
        if (newSalvage.compareTo(bookValue) > 0) {
            throw new ConflictException("新残值 " + newSalvage
                    + " 不得高于调整时点账面价值 " + bookValue);
        }

        ParameterAdjustment rec = new ParameterAdjustment(
                assetId, effective,
                asset.getPurchaseCost(), newCost,
                asset.getSalvageValue(), newSalvage,
                asset.getUsefulLifeMonths(), newLife,
                asset.getDepreciationMethod(), newMethod,
                asset.getDecliningRatePct(), newRate,
                bookValue, elapsed, newLife - elapsed,
                cmd.reason(), currentUser(), cmd.requestId());
        adjustments.save(rec);

        asset.setPurchaseCost(newCost);
        asset.setSalvageValue(newSalvage);
        asset.setUsefulLifeMonths(newLife);
        asset.setDepreciationMethod(newMethod);
        asset.setDecliningRatePct(newMethod == com.itasset.domain.DepreciationMethod.DECLINING_BALANCE
                ? newRate : null);
        return rec;
    }

    private String currentUser() {
        return UserContext.currentUser();
    }

    private static BigDecimal nvl(BigDecimal v, BigDecimal fallback) {
        return v == null ? fallback : v;
    }

    static void validateParams(BigDecimal cost, BigDecimal salvage, int life,
                               com.itasset.domain.DepreciationMethod method, BigDecimal ratePct) {
        if (cost.signum() <= 0) {
            throw new BusinessRuleException("采购成本必须大于 0");
        }
        if (salvage.signum() < 0 || salvage.compareTo(cost) > 0) {
            throw new BusinessRuleException("残值必须 >= 0 且 <= 采购成本");
        }
        if (life < 1 || life > 600) {
            throw new BusinessRuleException("使用月数必须在 1~600 之间");
        }
        if (method == com.itasset.domain.DepreciationMethod.DECLINING_BALANCE) {
            if (ratePct == null || ratePct.signum() <= 0 || ratePct.compareTo(new BigDecimal("100")) >= 0) {
                throw new BusinessRuleException("余额递减法年折旧率必须在 (0,100) 之间");
            }
        }
    }
}
