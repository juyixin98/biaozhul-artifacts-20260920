package dev.intervals.tz;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

class TzVersionTest {

    @Test
    @DisplayName("JVM tzdb 版本形如 2026x")
    void jvmVersionShape() {
        String v = TzVersion.jvmVersion();
        assertTrue(v.matches("\\d{4}[a-z]"), "实际版本: " + v);
    }

    @Test
    @DisplayName("info 包含全部版本字段且无 null 占位（unknown 也非 null）")
    void infoComplete() {
        Map<String, Object> info = TzVersion.info();
        for (Map.Entry<String, Object> e : info.entrySet()) {
            assertNotNull(e.getValue(), e.getKey());
        }
        assertTrue(info.containsKey("osTzdataVersion"));
        assertTrue(info.containsKey("jvmTzdbVersion"));
    }
}
