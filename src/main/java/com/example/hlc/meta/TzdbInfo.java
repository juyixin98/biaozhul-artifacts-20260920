package com.example.hlc.meta;

import java.time.ZoneId;
import java.time.zone.ZoneRulesProvider;
import java.util.NavigableMap;

/** Reports the version of the IANA time-zone database bundled with the runtime. */
public final class TzdbInfo {

    private TzdbInfo() {
    }

    /**
     * Returns the tzdb version (e.g. "2024a") by inspecting the zone-rule
     * versions known to the JVM's {@link ZoneRulesProvider}.
     */
    public static String tzdbVersion() {
        NavigableMap<String, ?> utcVersions = ZoneRulesProvider.getVersions("UTC");
        if (!utcVersions.isEmpty()) {
            return utcVersions.lastKey();
        }
        for (String zoneId : ZoneRulesProvider.getAvailableZoneIds()) {
            NavigableMap<String, ?> versions = ZoneRulesProvider.getVersions(zoneId);
            if (!versions.isEmpty()) {
                return versions.lastKey();
            }
        }
        return "unknown";
    }

    public static String systemZone() {
        return ZoneId.systemDefault().getId();
    }
}
