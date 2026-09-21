package com.example.itasset.web;

import com.example.itasset.service.AdjustmentService;
import com.example.itasset.support.IdempotentExecutor;
import com.example.itasset.support.IdempotencyService;
import com.example.itasset.web.dto.AdjustmentRequest;
import com.example.itasset.web.dto.AdjustmentResponse;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.Valid;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

/** Accounting parameter adjustments: FINANCE only. */
@RestController
@RequestMapping("/api/assets/{assetId}/adjustments")
public class AdjustmentController {

    private final AdjustmentService adjustmentService;
    private final IdempotentExecutor executor;
    private final ObjectMapper objectMapper;

    public AdjustmentController(AdjustmentService adjustmentService,
                                IdempotentExecutor executor,
                                ObjectMapper objectMapper) {
        this.adjustmentService = adjustmentService;
        this.executor = executor;
        this.objectMapper = objectMapper;
    }

    @GetMapping
    public List<AdjustmentResponse> history(@PathVariable Long assetId) {
        return adjustmentService.history(assetId).stream()
                .map(AdjustmentResponse::of)
                .toList();
    }

    @PostMapping
    @PreAuthorize("hasRole('FINANCE')")
    public ResponseEntity<?> adjust(@PathVariable Long assetId,
                                    @Valid @RequestBody AdjustmentRequest body,
                                    HttpServletRequest request,
                                    HttpServletResponse response) throws Exception {
        String requestId = request.getHeader(IdempotencyService.HEADER);
        IdempotentExecutor.Result<AdjustmentResponse> result = executor.execute(
                requestId,
                "ASSET_ADJUSTMENT:" + assetId,
                objectMapper.writeValueAsString(body),
                200,
                "ROLE_FINANCE",
                () -> {
                    var adjustment = adjustmentService.adjust(assetId, requestId, body);
                    return AdjustmentResponse.of(adjustment);
                });
        return AssetController.toResponse(result, response);
    }
}
