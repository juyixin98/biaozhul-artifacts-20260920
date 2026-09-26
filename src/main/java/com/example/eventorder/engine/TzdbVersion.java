package com.example.eventorder.engine;

import java.time.zone.ZoneRulesProvider;
import java.util.NavigableSet;
import java.util.TreeSet;

/**
 * Reports the IANA time-zone database version bundled with the running JDK.
 * The version is recorded in every response for reproducibility; it never
 * influences results (all computation happens on {@link java.time.Instant}s).
 */
public final class TzdbVersion {

    private TzdbVersion() {
    }

    public static String detect() {
        try {
            NavigableSet<String> versions = new TreeSet<>(
                    ZoneRulesProvider.getVersions("UTC").keySet());
            return versions.isEmpty() ? "unknown" : versions.last();
        } catch (RuntimeException e) {
            return "unknown";
        }
    }
}
