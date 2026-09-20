package com.itasset.service;

import java.time.LocalDate;
import java.time.YearMonth;
import java.time.format.DateTimeFormatter;

/** 会计期间工具：期间为 yyyyMM 字符串，可按字典序比较（等价于时间序）。 */
public final class Periods {

    private static final DateTimeFormatter FMT = DateTimeFormatter.ofPattern("yyyyMM");

    private Periods() {
    }

    public static String format(YearMonth ym) {
        return ym.format(FMT);
    }

    public static YearMonth parse(String period) {
        return YearMonth.parse(period, FMT);
    }

    /** 折旧首个期间：启用日期次月（当月增加次月起计提）。 */
    public static String firstDepreciationPeriod(LocalDate inServiceDate) {
        return format(YearMonth.from(inServiceDate).plusMonths(1));
    }

    public static String plus(String period, long months) {
        return format(parse(period).plusMonths(months));
    }

    public static boolean isAfter(String a, String b) {
        return a.compareTo(b) > 0;
    }

    public static boolean isBefore(String a, String b) {
        return a.compareTo(b) < 0;
    }

    /** 两个周期间相差的整月数（b - a）。 */
    public static long monthsBetween(String fromInclusive, String toInclusive) {
        return java.time.temporal.ChronoUnit.MONTHS.between(parse(fromInclusive), parse(toInclusive));
    }
}
