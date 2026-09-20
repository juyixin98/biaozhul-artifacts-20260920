package com.itasset.web;

import com.itasset.domain.DepreciationEntry;
import com.itasset.domain.ParameterAdjustment;
import com.itasset.domain.StatusChange;
import com.itasset.repo.DepreciationEntryRepository;
import com.itasset.repo.ParameterAdjustmentRepository;
import com.itasset.repo.StatusChangeRepository;
import com.itasset.service.AssetService;
import com.itasset.service.DepreciationService;
import com.itasset.service.ParameterAdjustmentService;
import com.itasset.service.StatusTransitionService;
import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import java.net.URI;
import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/assets")
public class AssetController {

    private final AssetService assets;
    private final StatusTransitionService transitions;
    private final DepreciationService depreciation;
    private final ParameterAdjustmentService adjustments;
    private final StatusChangeRepository changeRepo;
    private final DepreciationEntryRepository entryRepo;
    private final ParameterAdjustmentRepository adjustmentRepo;

    public AssetController(AssetService assets, StatusTransitionService transitions,
                           DepreciationService depreciation,
                           ParameterAdjustmentService adjustments,
                           StatusChangeRepository changeRepo,
                           DepreciationEntryRepository entryRepo,
                           ParameterAdjustmentRepository adjustmentRepo) {
        this.assets = assets;
        this.transitions = transitions;
        this.depreciation = depreciation;
        this.adjustments = adjustments;
        this.changeRepo = changeRepo;
        this.entryRepo = entryRepo;
        this.adjustmentRepo = adjustmentRepo;
    }

    @PostMapping
    public ResponseEntity<Dtos.AssetView> create(@Valid @RequestBody CreateAssetRequest req) {
        var saved = assets.create(req);
        return ResponseEntity.created(URI.create("/api/assets/" + saved.getId()))
                .body(Dtos.AssetView.of(saved));
    }

    @GetMapping
    public List<Dtos.AssetView> list() {
        return assets.list().stream().map(Dtos.AssetView::of).toList();
    }

    @GetMapping("/{id}")
    public Dtos.AssetView get(@PathVariable Long id) {
        return Dtos.AssetView.of(assets.get(id));
    }

    // ---- 状态转换 ----

    @PostMapping("/{id}/transitions")
    @ResponseStatus(HttpStatus.CREATED)
    public Map<String, Object> transition(@PathVariable Long id,
                                          @Valid @RequestBody TransitionRequest req) {
        var change = transitions.transition(id, req.targetStatus(), req.expectedVersion(),
                req.requestId(), req.reason(), com.itasset.service.UserContext.currentUser());
        var asset = assets.get(id);
        return Map.of(
                "change", Dtos.ChangeView.of(change),
                "currentStatus", asset.getStatus().name(),
                "currentVersion", asset.getVersion());
    }

    @GetMapping("/{id}/transitions")
    public List<Dtos.ChangeView> history(@PathVariable Long id) {
        assets.get(id);
        return changeRepo.findByAssetIdOrderByIdAsc(id).stream().map(Dtos.ChangeView::of).toList();
    }

    // ---- 折旧 ----

    @PostMapping("/{id}/depreciation/runs")
    @ResponseStatus(HttpStatus.CREATED)
    public Map<String, Object> runDepreciation(@PathVariable Long id,
                                               @Valid @RequestBody DepreciationRunRequest req) {
        var result = depreciation.runDepreciation(id, req.fromPeriod(), req.toPeriod(), req.requestId());
        return Map.of(
                "requestId", result.requestId(),
                "assetId", result.assetId(),
                "fromPeriod", result.fromPeriod(),
                "toPeriod", result.toPeriod(),
                "createdEntries", result.createdEntries().stream().map(Dtos.EntryView::of).toList(),
                "skippedPeriods", result.skippedPeriods());
    }

    @GetMapping("/{id}/depreciation/entries")
    public List<Dtos.EntryView> entries(@PathVariable Long id,
                                        @RequestParam(required = false) String fromPeriod,
                                        @RequestParam(required = false) String toPeriod) {
        assets.get(id);
        List<DepreciationEntry> list = fromPeriod == null
                ? entryRepo.findByAssetIdOrderByPeriodAsc(id)
                : entryRepo.findFromPeriod(id, fromPeriod);
        return list.stream()
                .filter(e -> toPeriod == null || e.getPeriod().compareTo(toPeriod) <= 0)
                .map(Dtos.EntryView::of).toList();
    }

    // ---- 参数调整 ----

    @PostMapping("/{id}/adjustments")
    @ResponseStatus(HttpStatus.CREATED)
    public Dtos.AdjustmentView adjust(@PathVariable Long id,
                                      @Valid @RequestBody AdjustmentRequest req) {
        var rec = adjustments.adjust(id, new ParameterAdjustmentService.AdjustmentCommand(
                req.effectivePeriod(), req.newPurchaseCost(), req.newSalvageValue(),
                req.newUsefulLifeMonths(), req.newDepreciationMethod(),
                req.newDecliningRatePct(), req.reason(), req.requestId()));
        return Dtos.AdjustmentView.of(rec);
    }

    @GetMapping("/{id}/adjustments")
    public List<Dtos.AdjustmentView> adjustments(@PathVariable Long id) {
        assets.get(id);
        return adjustmentRepo.findByAssetIdOrderByIdAsc(id).stream()
                .map(Dtos.AdjustmentView::of).toList();
    }
}
