package com.example.intervals.tz;

import org.junit.jupiter.api.Test;

import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class TimeZoneInfoTest {

    @Test
    void detectsJreTzDataVersion() {
        TimeZoneInfo info = TimeZoneInfo.detect();
        // IANA versions look like 2024a, 2026b ... never blank/unknown on a normal JDK.
        assertNotEquals("unknown", info.jreTzDataVersion());
        assertTrue(info.jreTzDataVersion().matches("20\\d{2}[a-z]"),
                "unexpected tzdata version: " + info.jreTzDataVersion());
        assertTrue(info.zoneCount() > 300);
    }

    @Test
    void parsesOsVersionFromZiFile() {
        // Point at the real Linux file when present; otherwise the code must
        // degrade gracefully to "unknown".
        Path real = Path.of("/usr/share/zoneinfo/tzdata.zi");
        TimeZoneInfo info = TimeZoneInfo.detect(real);
        if (java.nio.file.Files.isRegularFile(real)) {
            assertTrue(info.osTzDataVersion().matches("20\\d{2}[a-z]"),
                    "unexpected OS tzdata version: " + info.osTzDataVersion());
        } else {
            assertEquals("unknown", info.osTzDataVersion());
        }
    }

    @Test
    void unknownWhenOsFileMissing() {
        TimeZoneInfo info = TimeZoneInfo.detect(Path.of("/nonexistent/tzdata.zi"));
        assertEquals("unknown", info.osTzDataVersion());
    }

    @Test
    void recordsJavaRuntimeMetadata() {
        TimeZoneInfo info = TimeZoneInfo.detect();
        assertTrue(info.javaVersion().startsWith("21"));
        assertTrue(!info.javaVendor().isBlank());
        assertTrue(info.sampleZones().contains("UTC"));
    }
}
