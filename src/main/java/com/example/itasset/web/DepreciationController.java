package com.example.itasset.web;

import com.example.itasset.domain.DepreciationRun;
import com.example.itasset.service.DepreciationService;
import com.example.itasset.support.IdempotentExecutor;
import com.example.itasset.support.IdempotencyService;
import com.example.itasset.web.dto.DepreciationRunRequest;
import com.example.itasset.web.dto.DepreciationRunResponse;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.Valid;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/depreciation/runs")
public class DepreciationController {

    private final DepreciationService depreciationService;
    private final IdempotentExecutor executor;
    private final ObjectMapper objectMapper;

    public DepreciationController(DepreciationService depreciationService,
                                  IdempotentExecutor executor,
                                  ObjectMapper objectMapper) {
        this.depreciationService = depreciationService;
        this.executor = executor;
        this.objectMapper = objectMapper;
    }

    /**
     * Book depreciation for a period (and any unposted earlier eligible months).
     * Idempotent via X-Request-Id; a period already run books nothing twice.
     */
    @PostMapping
    @PreAuthorize("hasRole('FINANCE')")
    public ResponseEntity<?> run(@Valid @RequestBody DepreciationRunRequest body,
                                 HttpServletRequest request,
                                 HttpServletResponse response) throws Exception {
        String requestId = request.getHeader(IdempotencyService.HEADER);
        IdempotentExecutor.Result<DepreciationRunResponse> result = executor.execute(
                requestId,
                "DEPRECIATION_RUN",
                objectMapper.writeValueAsString(body),
                200,
                "ROLE_FINANCE",
                () -> {
                    DepreciationRun run = depreciationService.runPeriod(requestId, body.period());
                    return DepreciationRunResponse.of(run);
                });
        return AssetController.toResponse(result, response);
    }
}
