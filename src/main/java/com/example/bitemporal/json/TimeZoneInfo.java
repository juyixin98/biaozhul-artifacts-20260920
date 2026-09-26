package com.example.bitemporal.json;

import java.time.zone.ZoneRulesProvider;
import java.util.TreeSet;

/**
 * 运行环境与时区数据库版本信息。
 * TZDB 版本来自 JDK 内置的 IANA Time Zone Database（{@code java.time} 使用）。
 */
public record TimeZoneInfo(String tzdbVersion, String javaVersion, String defaultZone) {

    public static TimeZoneInfo current() {
        return new TimeZoneInfo(resolveTzdbVersion(),
                System.getProperty("java.version"),
                java.time.ZoneId.systemDefault().toString());
    }

    /**
     * TZDB 版本号（如 {@code 2024a}）。
     * {@link ZoneRulesProvider#getVersions(String)} 的键即版本标识。
     */
    static String resolveTzdbVersion() {
        try {
            TreeSet<String> versions = new TreeSet<>(
                    ZoneRulesProvider.getVersions("Etc/UTC").keySet());
            if (!versions.isEmpty()) {
                return versions.last();
            }
        } catch (RuntimeException ignored) {
            // 落到未知标记，绝不影响主流程
        }
        return "unknown";
    }
}
