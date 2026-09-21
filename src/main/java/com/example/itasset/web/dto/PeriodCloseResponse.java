package com.example.itasset.web.dto;

import java.time.LocalDateTime;

public record PeriodCloseResponse(
        String period,
        String closedBy,
        LocalDateTime closedAt,
        String note,
        boolean replayed
) {
}
