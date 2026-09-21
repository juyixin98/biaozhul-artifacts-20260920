package com.example.itasset.service;

import com.example.itasset.domain.PeriodClose;
import com.example.itasset.repo.PeriodCloseRepository;
import com.example.itasset.support.Periods;
import com.example.itasset.web.ApiException;
import org.springframework.stereotype.Service;

import java.time.Clock;
import java.time.YearMonth;
import java.util.List;

/**
 * Period close boundary. Closing a period locks it and every earlier period:
 * a period {@code p} is closed iff a row exists with {@code period >= p}
 * (periods are closed in strict chronological order, no gaps, no future closes).
 */
@Service
public class PeriodService {

    private final PeriodCloseRepository repository;
    private final Clock clock;

    public PeriodService(PeriodCloseRepository repository, Clock clock) {
        this.repository = repository;
        this.clock = clock;
    }

    /** Highest closed period, or null if none. */
    public String closedThrough() {
        return repository.findAll().stream()
                .map(PeriodClose::getPeriod)
                .max(String::compareTo)
                .orElse(null);
    }

    public boolean isClosed(String period) {
        String through = closedThrough();
        return through != null && period.compareTo(through) <= 0;
    }

    public void assertOpen(String period) {
        if (isClosed(period)) {
            throw ApiException.conflict("Period " + period + " is closed; its books are locked");
        }
    }

    public List<PeriodClose> all() {
        return repository.findAll().stream()
                .sorted((a, b) -> b.getPeriod().compareTo(a.getPeriod()))
                .toList();
    }

    /**
     * Close {@code period}: it must be open, must not be a future month, and must be the
     * exact next month after the current closed boundary (no skipping periods).
     */
    public PeriodClose close(String period, String username, String note) {
        Periods.parse(period);
        if (period.compareTo(YearMonth.now(clock).format(Periods.FORMAT)) > 0) {
            throw ApiException.unprocessable("Cannot close a future period: " + period);
        }
        String expected = nextOpenPeriod();
        if (expected != null && !period.equals(expected)) {
            throw ApiException.unprocessable(
                    "Periods must be closed without gaps; close " + expected + " first");
        }
        PeriodClose close = new PeriodClose(period, username, note);
        return repository.save(close);
    }

    /** The period that closing must target next (the month after the last close). */
    public String nextOpenPeriod() {
        String through = closedThrough();
        return through == null ? null : Periods.plusMonths(through, 1);
    }
}
