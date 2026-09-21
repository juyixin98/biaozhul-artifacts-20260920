package com.example.itasset.web.dto;

import com.example.itasset.domain.AssetTransition;

import java.time.LocalDateTime;

public record TransitionResponse(
        Long id,
        Long assetId,
        String requestId,
        String fromStatus,
        String toStatus,
        String effectivePeriod,
        String reason,
        long expectedVersion,
        long resultedVersion,
        LocalDateTime createdAt,
        boolean replayed,
        AssetResponse asset
) {
    public static TransitionResponse of(AssetTransition t, AssetResponse asset, boolean replayed) {
        return new TransitionResponse(
                t.getId(),
                t.getAssetId(),
                t.getRequestId(),
                t.getFromStatus() == null ? null : t.getFromStatus().name(),
                t.getToStatus().name(),
                t.getEffectivePeriod(),
                t.getReason(),
                t.getExpectedVersion(),
                t.getResultedVersion(),
                t.getCreatedAt(),
                replayed,
                asset);
    }
}
