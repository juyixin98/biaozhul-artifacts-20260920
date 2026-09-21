package com.example.itasset.web;

import com.example.itasset.domain.*;

import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;

public final class Views {

    private Views() {
    }

    public record AssetView(
            Long id,
            String assetCode,
            String name,
            String department,
            BigDecimal purchaseCost,
            BigDecimal salvageValue,
            LocalDate placedInService,
            Integer usefulLifeMonths,
            String depreciationMethod,
            String status,
            Long version,
            Instant createdAt) {

        public static AssetView of(Asset a) {
            return new AssetView(a.getId(), a.getAssetCode(), a.getName(), a.getDepartment(),
                    a.getPurchaseCost(), a.getSalvageValue(), a.getPlacedInService(),
                    a.getUsefulLifeMonths(), a.getDepreciationMethod().name(),
                    a.getStatus().name(), a.getVersion(), a.getCreatedAt());
        }
    }

    public record TransitionView(
            Long id,
            Long assetId,
            String requestId,
            String fromStatus,
            String toStatus,
            long expectedVersion,
            String note,
            String operator,
            Instant createdAt) {

        public static TransitionView of(StatusTransition t) {
            return new TransitionView(t.getId(), t.getAssetId(), t.getRequestId(),
                    t.getFromStatus() == null ? null : t.getFromStatus().name(),
                    t.getToStatus().name(), t.getExpectedVersion(),
                    t.getNote(), t.getOperator(), t.getCreatedAt());
        }
    }

    public record PolicyView(
            Long id,
            Integer sequenceNo,
            Integer effectivePeriod,
            BigDecimal purchaseCost,
            BigDecimal salvageValue,
            Integer usefulLifeMonths,
            String depreciationMethod,
            BigDecimal openingBookValue,
            Integer remainingLifeMonths,
            String reason,
            String adjustedBy,
            Instant createdAt) {

        public static PolicyView of(DepreciationPolicy p) {
            return new PolicyView(p.getId(), p.getSequenceNo(), p.getEffectivePeriod(),
                    p.getPurchaseCost(), p.getSalvageValue(), p.getUsefulLifeMonths(),
                    p.getDepreciationMethod().name(), p.getOpeningBookValue(),
                    p.getRemainingLifeMonths(), p.getReason(), p.getAdjustedBy(), p.getCreatedAt());
        }
    }

    public record EntryView(
            Long id,
            Long assetId,
            Integer period,
            BigDecimal openingValue,
            BigDecimal charge,
            BigDecimal closingValue,
            Long policyId,
            String calcDetail,
            Instant createdAt) {

        public static EntryView of(DepreciationEntry e) {
            return new EntryView(e.getId(), e.getAssetId(), e.getPeriod(),
                    e.getOpeningValue(), e.getCharge(), e.getClosingValue(),
                    e.getPolicyId(), e.getCalcDetail(), e.getCreatedAt());
        }
    }
}
