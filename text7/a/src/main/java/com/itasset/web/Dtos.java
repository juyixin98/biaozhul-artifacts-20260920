package com.itasset.web;

import com.itasset.domain.Asset;
import com.itasset.domain.DepreciationEntry;
import com.itasset.domain.ParameterAdjustment;
import com.itasset.domain.StatusChange;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.time.LocalDateTime;

/** 实体的只读 JSON 视图。 */
public final class Dtos {

    private Dtos() {
    }

    public record AssetView(Long id, String assetCode, String name, BigDecimal purchaseCost,
                            BigDecimal salvageValue, LocalDate inServiceDate, Integer usefulLifeMonths,
                            String department, String depreciationMethod, BigDecimal decliningRatePct,
                            String status, Long version, LocalDateTime createdAt) {
        public static AssetView of(Asset a) {
            return new AssetView(a.getId(), a.getAssetCode(), a.getName(), a.getPurchaseCost(),
                    a.getSalvageValue(), a.getInServiceDate(), a.getUsefulLifeMonths(),
                    a.getDepartment(), a.getDepreciationMethod().name(), a.getDecliningRatePct(),
                    a.getStatus().name(), a.getVersion(), a.getCreatedAt());
        }
    }

    public record ChangeView(Long id, Long assetId, String fromStatus, String toStatus,
                             Long expectedVersion, String requestId, String reason,
                             String changedBy, LocalDateTime createdAt) {
        public static ChangeView of(StatusChange c) {
            return new ChangeView(c.getId(), c.getAssetId(),
                    c.getFromStatus() == null ? null : c.getFromStatus().name(),
                    c.getToStatus().name(), c.getExpectedVersion(), c.getRequestId(),
                    c.getReason(), c.getChangedBy(), c.getCreatedAt());
        }
    }

    public record EntryView(Long id, Long assetId, String period, String depreciationMethod,
                            BigDecimal monthlyRatePct, BigDecimal openingBookValue,
                            BigDecimal depreciationAmount, BigDecimal closingBookValue,
                            BigDecimal costSnapshot, Integer lifeMonthsSnapshot,
                            Integer periodIndex, String requestId, LocalDateTime createdAt) {
        public static EntryView of(DepreciationEntry e) {
            return new EntryView(e.getId(), e.getAssetId(), e.getPeriod(),
                    e.getDepreciationMethod().name(), e.getMonthlyRatePct(),
                    e.getOpeningBookValue(), e.getDepreciationAmount(), e.getClosingBookValue(),
                    e.getCostSnapshot(), e.getLifeMonthsSnapshot(), e.getPeriodIndex(),
                    e.getRequestId(), e.getCreatedAt());
        }
    }

    public record AdjustmentView(Long id, Long assetId, String effectivePeriod,
                                 BigDecimal oldPurchaseCost, BigDecimal newPurchaseCost,
                                 BigDecimal oldSalvageValue, BigDecimal newSalvageValue,
                                 Integer oldUsefulLifeMonths, Integer newUsefulLifeMonths,
                                 String oldDepreciationMethod, String newDepreciationMethod,
                                 BigDecimal oldDecliningRatePct, BigDecimal newDecliningRatePct,
                                 BigDecimal bookValueAtAdjustment, Integer elapsedMonths,
                                 Integer segmentMonths, String reason, String adjustedBy,
                                 String requestId, LocalDateTime createdAt) {
        public static AdjustmentView of(ParameterAdjustment a) {
            return new AdjustmentView(a.getId(), a.getAssetId(), a.getEffectivePeriod(),
                    a.getOldPurchaseCost(), a.getNewPurchaseCost(),
                    a.getOldSalvageValue(), a.getNewSalvageValue(),
                    a.getOldUsefulLifeMonths(), a.getNewUsefulLifeMonths(),
                    a.getOldDepreciationMethod() == null ? null : a.getOldDepreciationMethod().name(),
                    a.getNewDepreciationMethod() == null ? null : a.getNewDepreciationMethod().name(),
                    a.getOldDecliningRatePct(), a.getNewDecliningRatePct(),
                    a.getBookValueAtAdjustment(), a.getElapsedMonths(), a.getSegmentMonths(),
                    a.getReason(), a.getAdjustedBy(), a.getRequestId(), a.getCreatedAt());
        }
    }
}
