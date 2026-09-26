package com.example.vic.engine;

import java.time.zone.ZoneRulesProvider;
import java.util.NavigableMap;

/**
 * Reports runtime metadata, including the IANA time-zone database version bundled
 * with the JVM. The engine itself computes on a plain long axis; when callers map
 * instants to that axis they must know which tzdb was in effect, so we record it.
 */
public final class MetaInfo {

    private MetaInfo() {
    }

    public static String tzdbVersion() {
        NavigableMap<String, ?> versions = ZoneRulesProvider.getVersions("UTC");
        if (versions.isEmpty()) {
            return "unknown";
        }
        return versions.lastKey();
    }

    public static String javaVersion() {
        return System.getProperty("java.version");
    }
}
