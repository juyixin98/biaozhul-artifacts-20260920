package com.example.migration.service;

import java.time.zone.ZoneRulesProvider;
import java.util.TreeSet;

/**
 * Identifies the IANA Time Zone Database (tzdata) version baked into the running
 * JDK. The version is exposed through {@link ZoneRulesProvider} version maps
 * (keys like {@code 2024a}, {@code 2026b}).
 */
public final class TzdbInfo {

    private TzdbInfo() {
    }

    /**
     * @return tzdb version id, e.g. {@code 2026b}; {@code unknown} if the JDK
     *         provider does not expose version ids
     */
    public static String version() {
        // UTC always exists and its rules change only on version stamps, so the
        // version map normally contains exactly one key: the bundled tzdb version.
        var versions = new TreeSet<>(ZoneRulesProvider.getVersions("UTC").keySet());
        if (versions.isEmpty()) {
            return "unknown";
        }
        return versions.last();
    }
}
