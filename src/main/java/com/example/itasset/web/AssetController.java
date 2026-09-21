package com.example.itasset.web;

import com.example.itasset.domain.HardwareAsset;
import com.example.itasset.service.AssetService;
import com.example.itasset.support.IdempotentExecutor;
import com.example.itasset.support.IdempotencyService;
import com.example.itasset.web.dto.AssetResponse;
import com.example.itasset.web.dto.CreateAssetRequest;
import com.example.itasset.web.dto.TransitionRequest;
import com.example.itasset.web.dto.TransitionResponse;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.Valid;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.PageRequest;
import org.springframework.data.domain.Sort;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@RestController
@RequestMapping("/api/assets")
public class AssetController {

    private final AssetService assetService;
    private final IdempotentExecutor executor;
    private final ObjectMapper objectMapper;

    public AssetController(AssetService assetService, IdempotentExecutor executor,
                           ObjectMapper objectMapper) {
        this.assetService = assetService;
        this.executor = executor;
        this.objectMapper = objectMapper;
    }

    @GetMapping
    @PreAuthorize("isAuthenticated()")
    public Page<AssetResponse> list(@RequestParam(required = false) String department,
                                    @RequestParam(defaultValue = "0") int page,
                                    @RequestParam(defaultValue = "20") int size) {
        PageRequest pr = PageRequest.of(page, Math.min(size, 200), Sort.by("id"));
        return assetService.list(department, pr).map(AssetResponse::of);
    }

    @GetMapping("/{id}")
    @PreAuthorize("isAuthenticated()")
    public AssetResponse get(@PathVariable Long id) {
        return AssetResponse.of(assetService.get(id));
    }

    @GetMapping("/{id}/transitions")
    @PreAuthorize("isAuthenticated()")
    public List<TransitionResponse> transitions(@PathVariable Long id) {
        return assetService.transitions(id).stream()
                .map(t -> TransitionResponse.of(t, AssetResponse.of(assetService.get(id)), false))
                .toList();
    }

    @PostMapping
    @PreAuthorize("hasAnyRole('ASSET_MANAGER','FINANCE')")
    public ResponseEntity<?> create(@Valid @RequestBody CreateAssetRequest body,
                                    HttpServletRequest request, HttpServletResponse response) throws Exception {
        boolean finance = request.isUserInRole("ROLE_FINANCE");
        String required = finance ? "ROLE_FINANCE" : "ROLE_ASSET_MANAGER";
        IdempotentExecutor.Result<AssetResponse> result = executor.execute(
                request.getHeader(IdempotencyService.HEADER),
                "ASSET_CREATE",
                objectMapper.writeValueAsString(body),
                201,
                required,
                () -> AssetResponse.of(assetService.create(body)));
        return toResponse(result, response);
    }

    /**
     * Generic whitelisted transition. {@code targetStatus} names the move; activation
     * parameters (usefulLifeMonths, effectiveDate) apply for IN_STOCK -> IN_USE.
     * Retire/disposal-only shorthand endpoints live on the finance-independent
     * lifecycle route.
     */
    @PostMapping("/{id}/transitions")
    @PreAuthorize("hasRole('ASSET_MANAGER')")
    public ResponseEntity<?> transition(@PathVariable Long id,
                                        @Valid @RequestBody TransitionRequest body,
                                        HttpServletRequest request,
                                        HttpServletResponse response) throws Exception {
        String requestId = request.getHeader(IdempotencyService.HEADER);
        IdempotentExecutor.Result<TransitionResponse> result = executor.execute(
                requestId,
                "ASSET_TRANSITION:" + id,
                objectMapper.writeValueAsString(body),
                200,
                "ROLE_ASSET_MANAGER",
                () -> {
                    var transition = assetService.transition(id, requestId, body);
                    return TransitionResponse.of(transition, AssetResponse.of(assetService.get(id)), false);
                });
        return toResponse(result, response);
    }

    static <T> ResponseEntity<?> toResponse(IdempotentExecutor.Result<T> result,
                                            HttpServletResponse response) {
        if (result.replayed()) {
            result.replay().writeReplay(response);
            return ResponseEntity.ok().build();
        }
        return ResponseEntity.status(result.status()).body(result.body());
    }
}
