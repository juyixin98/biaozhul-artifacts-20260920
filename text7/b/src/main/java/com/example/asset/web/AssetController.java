package com.example.asset.web;

import com.example.asset.domain.Asset;
import com.example.asset.repository.AssetRepository;
import com.example.asset.repository.AssetStatusTransitionRepository;
import com.example.asset.service.AssetService;
import com.example.asset.web.dto.*;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@Tag(name = "Assets", description = "资产建档与状态机")
@RestController
@RequestMapping("/api/assets")
public class AssetController {

    private final AssetService assetService;
    private final AssetRepository assetRepository;
    private final AssetStatusTransitionRepository transitionRepository;

    public AssetController(AssetService assetService, AssetRepository assetRepository,
                           AssetStatusTransitionRepository transitionRepository) {
        this.assetService = assetService;
        this.assetRepository = assetRepository;
        this.transitionRepository = transitionRepository;
    }

    @Operation(summary = "资产建档（资产经理）")
    @PostMapping
    @PreAuthorize("hasRole('ASSET_MANAGER')")
    public ResponseEntity<AssetResponse> create(@Valid @RequestBody CreateAssetRequest req) {
        Asset asset = new Asset();
        asset.setAssetCode(req.assetCode());
        asset.setName(req.name());
        asset.setPurchaseCost(req.purchaseCost());
        asset.setSalvageValue(req.salvageValue());
        asset.setCommissionDate(req.commissionDate());
        asset.setUsefulLifeMonths(req.usefulLifeMonths());
        asset.setDepartment(req.department());
        asset.setDepreciationMethod(req.depreciationMethod());
        return ResponseEntity.status(HttpStatus.CREATED).body(AssetResponse.of(assetService.create(asset)));
    }

    @Operation(summary = "资产列表")
    @GetMapping
    public List<AssetResponse> list() {
        return assetRepository.findAll().stream().map(AssetResponse::of).toList();
    }

    @Operation(summary = "资产详情")
    @GetMapping("/{id}")
    public AssetResponse get(@PathVariable Long id) {
        return AssetResponse.of(assetService.get(id));
    }

    @Operation(summary = "状态转换（资产经理）。携带 expectedVersion 与 requestId："
            + "重复 requestId 幂等返回首次结果；版本不匹配返回 409。")
    @PostMapping("/{id}/transitions")
    @PreAuthorize("hasRole('ASSET_MANAGER')")
    public ResponseEntity<TransitionResponse> transition(@PathVariable Long id,
                                                         @Valid @RequestBody TransitionRequest req,
                                                         @AuthenticationPrincipal Object principal) {
        String actor = principalName(principal);
        var result = assetService.transition(id, req.toStatus(), req.expectedVersion(), req.requestId(), actor);
        var body = TransitionResponse.of(result.transition(), result.currentVersion(), result.replayed());
        return ResponseEntity.status(result.replayed() ? HttpStatus.OK : HttpStatus.CREATED).body(body);
    }

    @Operation(summary = "状态变更历史（仅追加审计记录）")
    @GetMapping("/{id}/transitions")
    public List<TransitionResponse> transitions(@PathVariable Long id) {
        assetService.get(id);
        return transitionRepository.findByAssetIdOrderByIdAsc(id).stream()
                .map(t -> TransitionResponse.of(t, -1, false)).toList();
    }

    static String principalName(Object principal) {
        if (principal instanceof org.springframework.security.core.userdetails.UserDetails u) {
            return u.getUsername();
        }
        return principal != null ? principal.toString() : "anonymous";
    }
}
