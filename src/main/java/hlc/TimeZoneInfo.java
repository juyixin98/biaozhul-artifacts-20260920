package hlc;

import java.time.ZoneId;
import java.time.zone.ZoneRulesProvider;

/**
 * Small helper for recording the timezone database (tzdata/IANA) version in effect.
 *
 * <p>The HLC algorithm works in absolute physical time and is independent of timezones;
 * recording the tzdata version is purely provenance so persisted timestamps and any
 * human-readable renderings can be interpreted reproducibly.
 */
public final class TimeZoneInfo {

    private TimeZoneInfo() {
    }

    /**
     * Returns the IANA tzdb version shipped with the running JDK, e.g. {@code "2024a"}.
     * Falls back to {@code "unknown"} if the JVM does not expose the version.
     */
    public static String version() {
        try {
            String v = ZoneRulesProvider.getVersions("UTC").firstKey();
            if (v != null && !v.isBlank()) {
                return v;
            }
        } catch (RuntimeException ignored) {
            // fall through to unknown
        }
        return "unknown";
    }

    /** System default zone id, e.g. {@code Asia/Shanghai}. */
    public static String defaultZone() {
        return ZoneId.systemDefault().getId();
    }
}
