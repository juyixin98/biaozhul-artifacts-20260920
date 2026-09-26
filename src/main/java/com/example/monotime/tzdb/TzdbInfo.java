package com.example.monotime.tzdb;

import java.time.zone.ZoneRulesProvider;
import java.util.Set;

/**
 * 时区数据库（TZDB / IANA tzdata）版本信息。
 *
 * <p>每次转换结果都会记录所用 TZDB 版本，便于审计“某条超时是按哪一版夏令时规则算出的”。
 * 版本号来自 JDK 内置的 {@link ZoneRulesProvider}（例如 {@code 2026b}），
 * 数据文件位于 {@code $JAVA_HOME/lib/tzdb.dat}。</p>
 */
public record TzdbInfo(
        String tzdbVersion,
        String javaVersion,
        String javaRuntimeVersion,
        String vendor,
        String tzdbDataFile) {

    /** 内置 UTC 区域在任何 tzdata 版本中都存在，用它读取版本号最稳妥。 */
    private static final String VERSION_PROBE_ZONE = "UTC";

    public static TzdbInfo detect() {
        String javaHome = System.getProperty("java.home", "");
        return new TzdbInfo(
                detectTzdbVersion(),
                System.getProperty("java.version", "unknown"),
                System.getProperty("java.runtime.version", "unknown"),
                System.getProperty("java.vendor", "unknown"),
                javaHome + "/lib/tzdb.dat");
    }

    private static String detectTzdbVersion() {
        Set<String> versions = ZoneRulesProvider.getVersions(VERSION_PROBE_ZONE).keySet();
        if (versions.isEmpty()) {
            return "unknown";
        }
        return String.join(",", versions);
    }
}
