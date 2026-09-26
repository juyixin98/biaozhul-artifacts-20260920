package com.migration.planner.tz;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertTrue;

class TzdbInfoTest {

    @Test
    void reportsTzdbVersion() {
        String version = TzdbInfo.currentTzdbVersion();
        assertTrue(version.matches("\\d{4}[a-z]"),
                "expected a tzdb version like 2024a, got: " + version);
    }
}
