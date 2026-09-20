package com.example.asset.web;

import com.example.asset.repository.AssetAdjustmentRepository;
import com.example.asset.service.AdjustmentService;
import com.example.asset.service.AssetService;
import com.example.asset.web.dto.AdjustRequest;
import com.example.asset.web.dto.AdjustmentResponse;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@Tag(name = "Adjustments", description = "成本/使用年限的可追溯调整")
@RestController
@RequestMapping("/api/assets/{assetId}/adjustments")
public class AdjustmentController {

    private final AdjustmentService adjustmentService;
    private final AssetAdjustmentRepository adjustmentRepository;
    private final AssetService assetService;

    public AdjustmentController(AdjustmentService adjustmentService,
                                AssetAdjustmentRepository adjustmentRepository,
                                AssetService assetService) {
        this.adjustmentService = adjustmentService;
        this.adjustmentRepository = adjustmentRepository;
        this.assetService = assetService;
    }

    @Operation(summary = "调整成本/使用年限（财务）。未来适用：只影响未关账期间；保存原因与旧参数。")
    @PostMapping
    @PreAuthorize("hasRole('FINANCE')")
    public ResponseEntity<AdjustmentResponse> adjust(@PathVariable Long assetId,
                                                     @Valid @RequestBody AdjustRequest req,
                                                     @AuthenticationPrincipal Object principal) {
        var adjustment = adjustmentService.adjust(assetId, req.newCost(), req.newUsefulLifeMonths(),
                req.reason(), AssetController.principalName(principal));
        return ResponseEntity.status(HttpStatus.CREATED).body(AdjustmentResponse.of(adjustment));
    }

    @Operation(summary = "调整历史（含旧参数与原因）")
    @GetMapping
    public List<AdjustmentResponse> list(@PathVariable Long assetId) {
        assetService.get(assetId);
        return adjustmentRepository.findByAssetIdOrderByIdAsc(assetId).stream()
                .map(AdjustmentResponse::of).toList();
    }
}
