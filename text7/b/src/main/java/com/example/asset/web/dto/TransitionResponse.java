package com.example.asset.web.dto;

import com.example.asset.domain.AssetStatus;
import com.example.asset.domain.AssetStatusTransition;

import java.time.Instant;

public record TransitionResponse(
        Long transitionId,
        Long assetId,
        AssetStatus fromStatus,
        AssetStatus toStatus,
        String requestId,
        long currentVersion,
        boolean replayed,
        String actor,
        Instant createdAt
) {
    public static TransitionResponse of(AssetStatusTransition t, long currentVersion, boolean replayed) {
        return new TransitionResponse(t.getId(), t.getAssetId(), t.getFromStatus(), t.getToStatus(),
                t.getRequestId(), currentVersion, replayed, t.getActor(), t.getCreatedAt());
    }
}
