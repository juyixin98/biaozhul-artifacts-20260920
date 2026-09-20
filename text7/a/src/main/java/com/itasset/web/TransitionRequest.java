package com.itasset.web;

import com.itasset.domain.AssetStatus;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

public record TransitionRequest(
        @NotNull AssetStatus targetStatus,
        /** 客户端读取到的乐观锁版本；为空则不校验（不推荐）。 */
        Long expectedVersion,
        @NotBlank @Size(max = 64) String requestId,
        @Size(max = 500) String reason) {
}
