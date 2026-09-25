package dev.timeprecision;

import java.time.ZoneId;
import java.time.zone.ZoneRulesProvider;

/** Runtime metadata reported with every response, including the tz database version. */
public final class Meta {

    private Meta() {
    }

    /** IANA time zone database version bundled with the runtime (e.g. "2024a"). */
    public static String tzdbVersion() {
        var versions = ZoneRulesProvider.getVersions("UTC");
        return versions.isEmpty() ? "unknown" : versions.lastKey();
    }

    /** Number of zone IDs known to the runtime. */
    public static int zoneCount() {
        return ZoneId.getAvailableZoneIds().size();
    }

    /** Java runtime version string. */
    public static String javaVersion() {
        return System.getProperty("java.version");
    }
}
