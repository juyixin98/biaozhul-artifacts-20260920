package com.example.itasset.support;

/**
 * 会计期间工具：期间用整数 YYYYMM 表示（202609 表示 2026 年 9 月）。
 * 采用整月口径：启用当月即计提一整月，不按天数切分。
 */
public final class Periods {

    private Periods() {
    }

    public static int of(int year, int month) {
        return year * 100 + month;
    }

    public static int year(int period) {
        return period / 100;
    }

    public static int month(int period) {
        return period % 100;
    }

    /** 是否为合法的 YYYYMM 会计期间（月份 1..12，年份 4 位）。 */
    public static boolean isValid(int period) {
        int m = month(period);
        int y = year(period);
        return m >= 1 && m <= 12 && y >= 1000 && y <= 9999;
    }

    /** 上一个期间（返回 YYYYMM）。 */
    public static int previous(int period) {
        int y = year(period);
        int m = month(period);
        return m == 1 ? of(y - 1, 12) : of(y, m - 1);
    }

    /** 期间差（月）：monthsBetween(a, b) = b - a 的月数。b 早于 a 时为负。 */
    public static int monthsBetween(int earlier, int later) {
        return (year(later) - year(earlier)) * 12 + (month(later) - month(earlier));
    }

    /** 加上 n 个月（n 可为负）。 */
    public static int plusMonths(int period, int n) {
        int total = year(period) * 12 + (month(period) - 1) + n;
        int y = Math.floorDiv(total, 12);
        int m = Math.floorMod(total, 12) + 1;
        return of(y, m);
    }

    public static String format(int period) {
        return year(period) + "-" + String.format("%02d", month(period));
    }
}
