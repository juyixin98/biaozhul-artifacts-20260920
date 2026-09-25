package com.eventorder.engine;

import java.time.zone.ZoneRulesProvider;

/** Reports the IANA time-zone database version the JVM is running with. */
public final class TzdbInfo {

    private TzdbInfo() {
    }

    public static String version() {
        try {
            return ZoneRulesProvider.getVersions("UTC").lastKey();
        } catch (RuntimeException e) {
            return "unknown";
        }
    }
}
