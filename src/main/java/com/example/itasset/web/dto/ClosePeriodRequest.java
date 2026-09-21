package com.example.itasset.web.dto;

import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

public record ClosePeriodRequest(
        @NotNull @Pattern(regexp = "\\d{4}-\\d{2}") String period,
        @Size(max = 500) String note
) {
}
