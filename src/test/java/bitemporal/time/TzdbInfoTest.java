package bitemporal.time;

import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.Optional;

import org.junit.jupiter.api.Test;

class TzdbInfoTest {

    @Test
    void reportsBundledIanaTimeZoneDatabaseVersion() {
        Optional<String> version = TzdbInfo.version();
        assertTrue(version.isPresent(), "JDK-bundled tzdb version must be readable via public API");
        // IANA 版本形如 2026b、2025a；有些环境可能带后缀，这里只校验年份前缀。
        assertTrue(version.get().matches("\\d{4}.*"),
                "tzdb version should start with a 4-digit year, got: " + version.get());
    }

    @Test
    void reportsDefaultZoneAndLargeZoneSet() {
        assertNotNull(TzdbInfo.defaultZoneId());
        assertTrue(!TzdbInfo.defaultZoneId().isBlank());
        assertTrue(TzdbInfo.availableZoneCount() > 300,
                "IANA tzdb ships hundreds of zones, got " + TzdbInfo.availableZoneCount());
        assertTrue(TzdbInfo.summary().contains("tzdb version="));
    }
}
