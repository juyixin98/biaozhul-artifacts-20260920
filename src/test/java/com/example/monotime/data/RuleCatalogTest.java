package com.example.monotime.data;

import com.example.monotime.domain.TimeRule;
import org.junit.jupiter.api.Test;

import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class RuleCatalogTest {

    private final RuleCatalog catalog = new RuleCatalog();

    @Test
    void findsRuleByExactIdAndVersion() {
        // Act
        Optional<TimeRule> v1 = catalog.find("brief-absolute", "1");
        Optional<TimeRule> v2 = catalog.find("brief-absolute", "2");

        // Assert
        assertTrue(v1.isPresent());
        assertTrue(v2.isPresent());
        assertEquals("1", v1.get().version());
        assertEquals("2", v2.get().version());
    }

    @Test
    void findLatestReturnsHighestVersion() {
        // Act + Assert
        assertEquals("2", catalog.findLatest("brief-absolute").orElseThrow().version());
    }

    @Test
    void unknownRuleYieldsEmpty() {
        // Act + Assert
        assertTrue(catalog.find("nope", "1").isEmpty());
        assertTrue(catalog.findLatest("nope").isEmpty());
    }

    @Test
    void catalogIsPopulatedWithAllThreeRuleShapes() {
        // Act
        long absolute = catalog.all().stream().filter(r -> r instanceof TimeRule.AbsoluteInstant).count();
        long relative = catalog.all().stream().filter(r -> r instanceof TimeRule.RelativeDuration).count();
        long daily = catalog.all().stream().filter(r -> r instanceof TimeRule.DailyLocalCutoff).count();

        // Assert
        assertTrue(absolute >= 2);
        assertTrue(relative >= 1);
        assertTrue(daily >= 2);
    }

    @Test
    void relativeRuleCarriesTwoMinuteDuration() {
        // Act
        TimeRule rule = catalog.find("grace-two-minutes", "1").orElseThrow();

        // Assert：相对时长就是单调域语义，长度固定
        assertEquals(java.time.Duration.parse("PT2M"), ((TimeRule.RelativeDuration) rule).duration());
    }
}
