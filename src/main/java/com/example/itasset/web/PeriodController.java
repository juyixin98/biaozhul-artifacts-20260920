package com.example.itasset.web;

import com.example.itasset.domain.AccountingPeriod;
import com.example.itasset.repo.AccountingPeriodRepository;
import com.example.itasset.service.PeriodService;
import org.springframework.http.HttpStatus;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

import java.time.Instant;
import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/periods")
public class PeriodController {

    private final PeriodService periodService;
    private final AccountingPeriodRepository repository;

    public PeriodController(PeriodService periodService, AccountingPeriodRepository repository) {
        this.periodService = periodService;
        this.repository = repository;
    }

    /** 关账：仅财务。重复关账同一期间幂等。已关账期间不可再计提/重算。 */
    @PostMapping("/close")
    @PreAuthorize("hasRole('FINANCE')")
    @ResponseStatus(HttpStatus.CREATED)
    public Map<String, Object> close(@RequestBody Dtos.ClosePeriodRequest req, Authentication auth) {
        AccountingPeriod p = periodService.close(req.period(), auth.getName());
        return Map.of(
                "period", p.getPeriod(),
                "closed", true,
                "closedBy", p.getClosedBy(),
                "closedAt", p.getClosedAt().toString());
    }

    @GetMapping
    public List<Map<String, Object>> listClosed() {
        return repository.findAll().stream()
                .sorted((a, b) -> b.getPeriod() - a.getPeriod())
                .map(p -> Map.<String, Object>of(
                        "period", p.getPeriod(),
                        "closed", true,
                        "closedBy", p.getClosedBy(),
                        "closedAt", p.getClosedAt().toString()))
                .toList();
    }

    @GetMapping("/{period}")
    public Map<String, Object> status(@PathVariable Integer period) {
        return repository.findByPeriod(period)
                .map(p -> Map.<String, Object>of("period", period, "closed", true,
                        "closedBy", p.getClosedBy(), "closedAt", p.getClosedAt().toString()))
                .orElseGet(() -> Map.of("period", period, "closed", false, "checkedAt", Instant.now().toString()));
    }
}
