package com.example.intervals.tz;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Set;
import java.util.SortedSet;
import java.util.TreeSet;

/**
 * Reports the time-zone database versions in effect at run time.
 *
 * <p>Two distinct sources are recorded because they can differ (and do on the
 * build machine):
 * <ul>
 *   <li><b>JRE tzdata</b> — the IANA database embedded in the running JDK and
 *       used by {@code java.time}, read via
 *       {@link java.time.zone.ZoneRulesProvider#getVersions(String)};</li>
 *   <li><b>OS tzdata</b> — the operating system zone database, read on Linux
 *       from {@code /usr/share/zoneinfo/tzdata.zi} ({@code # version YYYYx}).</li>
 * </ul>
 * The component never shells out and degrades to {@code "unknown"} when an OS
 * database file is unavailable.
 */
public final class TimeZoneInfo {

    private static final SortedSet<String> SAMPLE_ZONES = new TreeSet<>(Set.of(
            "UTC", "GMT", "Etc/UTC",
            "Europe/London", "Europe/Paris", "Europe/Berlin",
            "America/New_York", "America/Los_Angeles", "America/Sao_Paulo",
            "Africa/Cairo", "Asia/Tokyo", "Asia/Shanghai", "Asia/Kolkata",
            "Australia/Sydney", "Pacific/Auckland"));

    private final String jreTzDataVersion;
    private final String osTzDataVersion;
    private final String javaVersion;
    private final String javaVendor;
    private final int zoneCount;

    private TimeZoneInfo(String jreTzDataVersion, String osTzDataVersion,
                         String javaVersion, String javaVendor, int zoneCount) {
        this.jreTzDataVersion = jreTzDataVersion;
        this.osTzDataVersion = osTzDataVersion;
        this.javaVersion = javaVersion;
        this.javaVendor = javaVendor;
        this.zoneCount = zoneCount;
    }

    public static TimeZoneInfo detect() {
        return detect(Path.of("/usr/share/zoneinfo/tzdata.zi"));
    }

    static TimeZoneInfo detect(Path osTzDataFile) {
        return new TimeZoneInfo(
                readJreTzDataVersion(),
                readOsTzDataVersion(osTzDataFile),
                System.getProperty("java.version", "unknown"),
                System.getProperty("java.vendor", "unknown"),
                java.time.ZoneId.getAvailableZoneIds().size());
    }

    private static String readJreTzDataVersion() {
        try {
            Set<String> versions =
                    java.time.zone.ZoneRulesProvider.getVersions("America/New_York").keySet();
            return versions.isEmpty() ? "unknown" : versions.iterator().next();
        } catch (RuntimeException e) {
            return "unknown";
        }
    }

    private static String readOsTzDataVersion(Path file) {
        if (!Files.isRegularFile(file)) {
            return "unknown";
        }
        try {
            for (String line : Files.readAllLines(file)) {
                String trimmed = line.trim();
                if (trimmed.startsWith("#") && trimmed.contains("version")) {
                    String[] parts = trimmed.split("\\s+");
                    for (int i = 0; i < parts.length - 1; i++) {
                        if (parts[i].equals("version")) {
                            return parts[i + 1];
                        }
                    }
                }
                if (!trimmed.startsWith("#")) {
                    break; // header is over; do not scan the whole file
                }
            }
        } catch (IOException | RuntimeException e) {
            return "unknown";
        }
        return "unknown";
    }

    public String jreTzDataVersion() {
        return jreTzDataVersion;
    }

    public String osTzDataVersion() {
        return osTzDataVersion;
    }

    public String javaVersion() {
        return javaVersion;
    }

    public String javaVendor() {
        return javaVendor;
    }

    public int zoneCount() {
        return zoneCount;
    }

    public SortedSet<String> sampleZones() {
        return SAMPLE_ZONES;
    }
}
