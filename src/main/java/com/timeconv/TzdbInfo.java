package com.timeconv;

import java.time.zone.ZoneRulesProvider;

/** Reports the time zone database version bundled with the running JVM. */
public final class TzdbInfo {

    private TzdbInfo() {
    }

    /**
     * The IANA tzdb version in use, e.g. "2026b". Derived from the versioned rules
     * of a zone that exists in every tzdb release.
     */
    public static String version() {
        try {
            return ZoneRulesProvider.getVersions("UTC").lastKey();
        } catch (Exception e) {
            return "unknown";
        }
    }
}
