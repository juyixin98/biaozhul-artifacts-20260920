package com.example.asset.web;

import com.example.asset.domain.FiscalPeriod;
import com.example.asset.service.PeriodService;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.*;

import java.time.Instant;
import java.time.YearMonth;

@Tag(name = "Periods", description = "会计期间与关账")
@RestController
@RequestMapping("/api/periods")
public class PeriodController {

    private final PeriodService periodService;

    public PeriodController(PeriodService periodService) {
        this.periodService = periodService;
    }

    public record PeriodResponse(String period, String status, Instant closedAt, String closedBy) {
        static PeriodResponse of(FiscalPeriod p) {
            return new PeriodResponse(p.getPeriod(), p.getStatus().name(), p.getClosedAt(), p.getClosedBy());
        }
    }

    @Operation(summary = "查询期间状态")
    @GetMapping("/{period}")
    public PeriodResponse get(@PathVariable String period) {
        return PeriodResponse.of(periodService.get(DepreciationController.parsePeriod(period)));
    }

    @Operation(summary = "关账（财务）。幂等；关账后该期间不可再计提或重算。")
    @PostMapping("/{period}/close")
    @PreAuthorize("hasRole('FINANCE')")
    public PeriodResponse close(@PathVariable String period, @AuthenticationPrincipal Object principal) {
        YearMonth ym = DepreciationController.parsePeriod(period);
        return PeriodResponse.of(periodService.close(ym, AssetController.principalName(principal)));
    }
}
