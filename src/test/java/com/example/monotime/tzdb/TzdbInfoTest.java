package com.example.monotime.tzdb;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class TzdbInfoTest {

    @Test
    void detectReturnsPlausibleTzdbVersion() {
        // Act
        TzdbInfo info = TzdbInfo.detect();

        // Assert：IANA tzdata 版本形如 2026b
        assertTrue(info.tzdbVersion().matches("\\d{4}[a-z]?"), "实际版本: " + info.tzdbVersion());
        assertNotEquals("unknown", info.tzdbVersion());
    }

    @Test
    void detectIncludesJavaAndDataFileInfo() {
        // Act
        TzdbInfo info = TzdbInfo.detect();

        // Assert
        assertTrue(info.javaVersion().startsWith("21"));
        assertTrue(info.tzdbDataFile().endsWith("/lib/tzdb.dat"));
        assertEquals(info.tzdbVersion(), TzdbInfo.detect().tzdbVersion());
    }
}
