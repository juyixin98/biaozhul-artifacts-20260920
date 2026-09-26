package com.example.vercov.meta;

import java.time.ZoneId;
import java.time.zone.ZoneRulesProvider;
import java.util.NavigableMap;
import java.time.zone.ZoneRules;

/**
 * Reports the IANA time-zone database version bundled with the running JVM.
 * Time-axis inputs are plain epoch seconds (UTC); this record documents which
 * tzdb release the runtime would use for any zone-aware interpretation.
 */
public final class TzdbInfo {

    private TzdbInfo() {
    }

    public static String tzdbVersion() {
        NavigableMap<String, ZoneRules> versions = ZoneRulesProvider.getVersions("UTC");
        return versions.isEmpty() ? "unknown" : versions.lastKey();
    }

    public static int availableZoneCount() {
        return ZoneId.getAvailableZoneIds().size();
    }
}
