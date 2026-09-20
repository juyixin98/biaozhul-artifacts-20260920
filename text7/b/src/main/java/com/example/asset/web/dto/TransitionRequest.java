package com.example.asset.web.dto;

import com.example.asset.domain.AssetStatus;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

public record TransitionRequest(
        @NotNull AssetStatus toStatus,
        @NotNull Long expectedVersion,
        @NotBlank @Size(max = 64) String requestId
) {
}
