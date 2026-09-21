package com.example.itasset.web;

import com.example.itasset.domain.AssetStatus;
import com.example.itasset.repo.DepreciationEntryRepository;
import com.example.itasset.repo.DepreciationPolicyRepository;
import com.example.itasset.repo.StatusTransitionRepository;
import com.example.itasset.service.AssetLifecycleService;
import com.example.itasset.service.DepreciationService;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/assets")
public class AssetController {

    private final AssetLifecycleService lifecycle;
    private final DepreciationService depreciation;
    private final StatusTransitionRepository transitions;
    private final DepreciationEntryRepository entries;
    private final DepreciationPolicyRepository policies;

    public AssetController(AssetLifecycleService lifecycle,
                           DepreciationService depreciation,
                           StatusTransitionRepository transitions,
                           DepreciationEntryRepository entries,
                           DepreciationPolicyRepository policies) {
        this.lifecycle = lifecycle;
        this.depreciation = depreciation;
        this.transitions = transitions;
        this.entries = entries;
        this.policies = policies;
    }

    /** 建档：仅资产经理。 */
    @PostMapping
    @PreAuthorize("hasRole('MANAGER')")
    @ResponseStatus(HttpStatus.CREATED)
    public Views.AssetView create(@RequestBody Dtos.CreateAssetRequest req, Authentication auth) {
        return Views.AssetView.of(lifecycle.create(req, auth.getName()));
    }

    /** 列表：任意已认证角色（含只读查看者）。 */
    @GetMapping
    public List<Views.AssetView> list() {
        return lifecycle.listAssets().stream().map(Views.AssetView::of).toList();
    }

    @GetMapping("/{id}")
    public Views.AssetView get(@PathVariable Long id) {
        return Views.AssetView.of(lifecycle.requireAsset(id));
    }

    // ---- 状态转换：仅资产经理 ----------------------------------------

    @PostMapping("/{id}/transitions/{target}")
    @PreAuthorize("hasRole('MANAGER')")
    public Views.AssetView transition(@PathVariable Long id,
                                      @PathVariable AssetStatus target,
                                      @RequestBody Dtos.TransitionRequest req,
                                      Authentication auth) {
        return Views.AssetView.of(lifecycle.transition(id, target, req, auth.getName()));
    }

    @GetMapping("/{id}/transitions")
    public List<Views.TransitionView> history(@PathVariable Long id) {
        lifecycle.requireAsset(id);
        return transitions.findByAssetIdOrderByIdAsc(id).stream()
                .map(Views.TransitionView::of).toList();
    }

    // ---- 折旧：计提（资产经理），查询（只读） ------------------------

    @PostMapping("/depreciation/post")
    @PreAuthorize("hasRole('MANAGER')")
    public Map<String, Object> postDepreciation(@RequestBody Dtos.PostDepreciationRequest req) {
        List<DepreciationService.AssetPostResult> results = depreciation.postPeriod(req.period());
        return Map.of(
                "period", req.period(),
                "total", results.size(),
                "posted", results.stream().filter(r -> r.outcome().equals("POSTED")).count(),
                "alreadyPosted", results.stream().filter(r -> r.outcome().equals("ALREADY_POSTED")).count(),
                "fullyDepreciated", results.stream().filter(r -> r.outcome().equals("FULLY_DEPRECIATED")).count(),
                "rejected", results.stream().filter(r -> r.outcome().equals("REJECTED")).count(),
                "results", results);
    }

    @GetMapping("/{id}/depreciation/entries")
    public List<Views.EntryView> entries(@PathVariable Long id) {
        lifecycle.requireAsset(id);
        return entries.findByAssetIdOrderByPeriodAsc(id).stream()
                .map(Views.EntryView::of).toList();
    }

    @GetMapping("/{id}/depreciation/policies")
    @PreAuthorize("hasAnyRole('FINANCE','MANAGER')")
    public List<Views.PolicyView> policyChain(@PathVariable Long id) {
        lifecycle.requireAsset(id);
        return policies.findByAssetIdOrderBySequenceNoAsc(id).stream()
                .map(Views.PolicyView::of).toList();
    }

    // ---- 可追溯调整：仅财务 ------------------------------------------

    @PostMapping("/{id}/adjustments")
    @PreAuthorize("hasRole('FINANCE')")
    @ResponseStatus(HttpStatus.CREATED)
    public Views.PolicyView adjust(@PathVariable Long id,
                                   @RequestBody Dtos.AdjustRequest req,
                                   Authentication auth) {
        return Views.PolicyView.of(depreciation.adjust(id, req, auth.getName()));
    }
}
