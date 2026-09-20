package com.itasset.web;

import com.itasset.domain.PeriodClose;
import com.itasset.service.PeriodCloseService;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/periods")
public class PeriodController {

    private final PeriodCloseService closes;

    public PeriodController(PeriodCloseService closes) {
        this.closes = closes;
    }

    /** 关账指定期间（FINANCE）。 */
    @PostMapping("/{period}/close")
    @ResponseStatus(HttpStatus.CREATED)
    public Map<String, Object> close(@PathVariable String period) {
        PeriodClose c = closes.close(period, com.itasset.service.UserContext.currentUser());
        return Map.of("period", c.getPeriod(), "closedBy", c.getClosedBy(),
                "closedAt", c.getClosedAt());
    }

    /** 已关账期间列表 + 指定期间是否冻结。 */
    @GetMapping("/closes")
    public Map<String, Object> list(@org.springframework.web.bind.annotation.RequestParam(required = false)
                                    String period) {
        List<PeriodClose> all = closes.list();
        return Map.of(
                "closedPeriods", all.stream().map(c -> Map.of(
                        "period", c.getPeriod(),
                        "closedBy", c.getClosedBy(),
                        "closedAt", c.getClosedAt().toString())).toList(),
                "queriedPeriod", period == null ? "" : period,
                "closed", period == null ? false : closes.isClosed(period),
                "serverTime", OffsetDateTime.now().toString());
    }
}
