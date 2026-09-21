package com.example.itasset.web.dto;

import com.example.itasset.domain.DepreciationRun;

import java.math.BigDecimal;
import java.time.LocalDateTime;

public record DepreciationRunResponse(
        Long runId,
        String requestId,
        String period,
        String status,
        int assetsPosted,
        int entriesCreated,
        BigDecimal totalCharge,
        String message,
        LocalDateTime finishedAt
) {
    public static DepreciationRunResponse of(DepreciationRun run) {
        return new DepreciationRunResponse(
                run.getId(),
                run.getRequestId(),
                run.getPeriod(),
                run.getStatus(),
                run.getAssetsPosted(),
                run.getEntriesCreated(),
                run.getTotalCharge(),
                run.getMessage(),
                run.getFinishedAt());
    }
}
