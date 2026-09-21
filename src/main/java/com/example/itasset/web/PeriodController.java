package com.example.itasset.web;

import com.example.itasset.service.PeriodCloseService;
import com.example.itasset.support.IdempotentExecutor;
import com.example.itasset.support.IdempotencyService;
import com.example.itasset.web.dto.ClosePeriodRequest;
import com.example.itasset.web.dto.PeriodCloseResponse;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.Valid;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@RestController
@RequestMapping("/api/periods")
public class PeriodController {

    private final PeriodCloseService closeService;
    private final IdempotentExecutor executor;
    private final ObjectMapper objectMapper;

    public PeriodController(PeriodCloseService closeService,
                            IdempotentExecutor executor,
                            ObjectMapper objectMapper) {
        this.closeService = closeService;
        this.executor = executor;
        this.objectMapper = objectMapper;
    }

    @GetMapping
    public List<PeriodCloseResponse> list() {
        return closeService.list().stream()
                .map(c -> new PeriodCloseResponse(c.getPeriod(), c.getClosedBy(),
                        c.getClosedAt(), c.getNote(), false))
                .toList();
    }

    /** Close a month: books lock at and before it. FINANCE only. */
    @PostMapping("/close")
    @PreAuthorize("hasRole('FINANCE')")
    public ResponseEntity<?> close(@Valid @RequestBody ClosePeriodRequest body,
                                   Authentication authentication,
                                   HttpServletRequest request,
                                   HttpServletResponse response) throws Exception {
        String requestId = request.getHeader(IdempotencyService.HEADER);
        IdempotentExecutor.Result<PeriodCloseResponse> result = executor.execute(
                requestId,
                "PERIOD_CLOSE",
                objectMapper.writeValueAsString(body),
                200,
                "ROLE_FINANCE",
                () -> {
                    var closed = closeService.close(body.period(), authentication.getName(), body.note());
                    return new PeriodCloseResponse(closed.getPeriod(), closed.getClosedBy(),
                            closed.getClosedAt(), closed.getNote(), false);
                });
        return AssetController.toResponse(result, response);
    }
}
