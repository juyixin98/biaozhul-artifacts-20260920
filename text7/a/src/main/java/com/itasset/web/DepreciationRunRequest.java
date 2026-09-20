package com.itasset.web;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;

public record DepreciationRunRequest(
        @NotBlank @Pattern(regexp = "\\d{6}") String fromPeriod,
        @NotBlank @Pattern(regexp = "\\d{6}") String toPeriod,
        @NotBlank String requestId) {
}
