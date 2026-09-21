package com.example.itasset.web.dto;

import com.example.itasset.domain.AssetStatus;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

import java.time.LocalDate;

/**
 * State transition request. {@code expectedVersion} is the version read by the client;
 * the transition succeeds only if it still matches. {@code requestId} is the
 * X-Request-Id header, not part of the body.
 */
public record TransitionRequest(
        @NotNull AssetStatus targetStatus,
        @NotNull Long expectedVersion,
        LocalDate effectiveDate,
        @Pattern(regexp = "\\d{4}-\\d{2}") String effectivePeriod,
        Integer usefulLifeMonths,
        @Size(max = 500) String reason
) {
}
