package com.example.itasset.support;

import java.time.LocalDate;
import java.time.YearMonth;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;

/** Accounting periods are calendar months formatted as {@code YYYY-MM}. */
public final class Periods {

    public static final DateTimeFormatter FORMAT = DateTimeFormatter.ofPattern("yyyy-MM");

    private Periods() {
    }

    public static String of(YearMonth ym) {
        return ym.format(FORMAT);
    }

    public static String of(LocalDate date) {
        return YearMonth.from(date).format(FORMAT);
    }

    public static YearMonth parse(String period) {
        try {
            return YearMonth.parse(period, FORMAT);
        } catch (DateTimeParseException e) {
            throw new IllegalArgumentException("Period must be in YYYY-MM format: " + period, e);
        }
    }

    public static String plusMonths(String period, long months) {
        return parse(period).plusMonths(months).format(FORMAT);
    }

    /** Period immediately following the month of the given date (Chinese GAAP: 当月增加，次月计提). */
    public static String firstDepreciationPeriod(LocalDate inServiceDate) {
        return YearMonth.from(inServiceDate).plusMonths(1).format(FORMAT);
    }

    public static String current() {
        return YearMonth.now().format(FORMAT);
    }
}
