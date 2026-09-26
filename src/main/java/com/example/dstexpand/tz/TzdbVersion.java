package com.example.dstexpand.tz;

import java.time.zone.ZoneRulesProvider;
import java.util.NavigableMap;

/**
 * Reports the IANA time-zone database version backing this JVM, so every
 * expansion response is traceable to the exact tz rules that produced it.
 */
public final class TzdbVersion {

    private TzdbVersion() {
    }

    /**
     * Returns the newest tzdb version id known to the JVM's zone rules
     * provider, e.g. "2024a". Falls back to "unknown" if the provider does
     * not expose version information.
     */
    public static String current() {
        try {
            NavigableMap<String, ?> versions = ZoneRulesProvider.getVersions("UTC");
            if (versions == null || versions.isEmpty()) {
                return "unknown";
            }
            return versions.lastKey();
        } catch (RuntimeException e) {
            return "unknown";
        }
    }
}
