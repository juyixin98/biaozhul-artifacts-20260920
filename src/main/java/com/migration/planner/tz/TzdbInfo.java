package com.migration.planner.tz;

import java.time.zone.ZoneRules;
import java.time.zone.ZoneRulesProvider;
import java.util.NavigableMap;

/** Reports the timezone database (tzdata) version of the running JVM. */
public final class TzdbInfo {

    private TzdbInfo() {
    }

    /** Latest tzdb version key known to the JVM, e.g. "2024a"; "unknown" if unavailable. */
    public static String currentTzdbVersion() {
        try {
            NavigableMap<String, ZoneRules> versions = ZoneRulesProvider.getVersions("UTC");
            return versions.isEmpty() ? "unknown" : versions.lastKey();
        } catch (RuntimeException e) {
            return "unknown";
        }
    }
}
