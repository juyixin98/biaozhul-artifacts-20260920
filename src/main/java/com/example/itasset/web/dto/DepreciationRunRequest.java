package com.example.itasset.web.dto;

import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;

public record DepreciationRunRequest(
        @NotNull @Pattern(regexp = "\\d{4}-\\d{2}") String period
) {
}
