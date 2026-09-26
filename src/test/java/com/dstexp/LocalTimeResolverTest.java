package com.dstexp;

import com.dstexp.model.GapStrategy;
import com.dstexp.model.OverlapStrategy;
import com.dstexp.schedule.LocalTimeResolver;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.time.LocalDateTime;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.time.zone.ZoneRules;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class LocalTimeResolverTest {

    private static final ZoneRules NY = ZoneId.of("America/New_York").getRules();

    // Spring forward 2026 in New York: 02:00 (-05:00) -> 03:00 (-04:00), so 02:30 is missing.
    private static final LocalDateTime GAP_TIME = LocalDateTime.parse("2026-03-08T02:30");
    // Fall back 2026: 02:00 (-04:00) -> 01:00 (-05:00), so 01:30 occurs twice.
    private static final LocalDateTime OVERLAP_TIME = LocalDateTime.parse("2026-11-01T01:30");

    @Test
    @DisplayName("normal local time maps with the single valid offset")
    void resolvesNormalTime() {
        var result = LocalTimeResolver.resolve(
                LocalDateTime.parse("2026-06-15T12:00"), NY,
                GapStrategy.ERROR, OverlapStrategy.ERROR).orElseThrow();

        assertThat(result.instant().toString()).isEqualTo("2026-06-15T16:00:00Z");
        assertThat(result.offset()).isEqualTo(ZoneOffset.ofHours(-4));
        assertThat(result.kind()).isEqualTo(LocalTimeResolver.Kind.NORMAL);
    }

    @Test
    @DisplayName("gap boundary instants resolve as normal times")
    void gapBoundariesAreNormal() {
        var before = LocalTimeResolver.resolve(
                LocalDateTime.parse("2026-03-08T01:59"), NY,
                GapStrategy.ERROR, OverlapStrategy.ERROR).orElseThrow();
        var after = LocalTimeResolver.resolve(
                LocalDateTime.parse("2026-03-08T03:00"), NY,
                GapStrategy.ERROR, OverlapStrategy.ERROR).orElseThrow();

        assertThat(before.kind()).isEqualTo(LocalTimeResolver.Kind.NORMAL);
        assertThat(after.kind()).isEqualTo(LocalTimeResolver.Kind.NORMAL);
        assertThat(after.instant().toString()).isEqualTo("2026-03-08T07:00:00Z");
    }

    @Test
    @DisplayName("gap EARLIER yields the earlier UTC instant with the post-gap offset")
    void gapEarlier() {
        var result = LocalTimeResolver.resolve(GAP_TIME, NY, GapStrategy.EARLIER, OverlapStrategy.LATER)
                .orElseThrow();
        // 02:30 interpreted at -04:00 -> 06:30Z (before the 07:00Z jump).
        assertThat(result.instant().toString()).isEqualTo("2026-03-08T06:30:00Z");
        assertThat(result.offset()).isEqualTo(ZoneOffset.ofHours(-4));
        assertThat(result.kind()).isEqualTo(LocalTimeResolver.Kind.GAP_EARLIER);
    }

    @Test
    @DisplayName("gap LATER yields the later UTC instant with the pre-gap offset")
    void gapLater() {
        var result = LocalTimeResolver.resolve(GAP_TIME, NY, GapStrategy.LATER, OverlapStrategy.LATER)
                .orElseThrow();
        // 02:30 interpreted at -05:00 -> 07:30Z (after the 07:00Z jump).
        assertThat(result.instant().toString()).isEqualTo("2026-03-08T07:30:00Z");
        assertThat(result.offset()).isEqualTo(ZoneOffset.ofHours(-5));
        assertThat(result.kind()).isEqualTo(LocalTimeResolver.Kind.GAP_LATER);
    }

    @Test
    @DisplayName("gap SKIP returns empty")
    void gapSkip() {
        assertThat(LocalTimeResolver.resolve(GAP_TIME, NY, GapStrategy.SKIP, OverlapStrategy.LATER))
                .isEmpty();
    }

    @Test
    @DisplayName("gap ERROR throws an exception flagged as a gap")
    void gapError() {
        assertThatThrownBy(() ->
                LocalTimeResolver.resolve(GAP_TIME, NY, GapStrategy.ERROR, OverlapStrategy.LATER))
                .isInstanceOf(LocalTimeResolver.AmbiguousTimeException.class)
                .satisfies(e -> assertThat(((LocalTimeResolver.AmbiguousTimeException) e).gap).isTrue());
    }

    @Test
    @DisplayName("overlap EARLIER yields the first pass at the summer offset")
    void overlapEarlier() {
        var result = LocalTimeResolver.resolve(OVERLAP_TIME, NY, GapStrategy.EARLIER, OverlapStrategy.EARLIER)
                .orElseThrow();
        // 01:30 at -04:00 -> 05:30Z.
        assertThat(result.instant().toString()).isEqualTo("2026-11-01T05:30:00Z");
        assertThat(result.offset()).isEqualTo(ZoneOffset.ofHours(-4));
        assertThat(result.kind()).isEqualTo(LocalTimeResolver.Kind.OVERLAP_EARLIER);
    }

    @Test
    @DisplayName("overlap LATER yields the second pass at the standard offset")
    void overlapLater() {
        var result = LocalTimeResolver.resolve(OVERLAP_TIME, NY, GapStrategy.EARLIER, OverlapStrategy.LATER)
                .orElseThrow();
        // 01:30 at -05:00 -> 06:30Z.
        assertThat(result.instant().toString()).isEqualTo("2026-11-01T06:30:00Z");
        assertThat(result.offset()).isEqualTo(ZoneOffset.ofHours(-5));
        assertThat(result.kind()).isEqualTo(LocalTimeResolver.Kind.OVERLAP_LATER);
    }

    @Test
    @DisplayName("overlap SKIP returns empty")
    void overlapSkip() {
        assertThat(LocalTimeResolver.resolve(OVERLAP_TIME, NY, GapStrategy.EARLIER, OverlapStrategy.SKIP))
                .isEmpty();
    }

    @Test
    @DisplayName("overlap ERROR throws an exception flagged as an overlap")
    void overlapError() {
        assertThatThrownBy(() ->
                LocalTimeResolver.resolve(OVERLAP_TIME, NY, GapStrategy.EARLIER, OverlapStrategy.ERROR))
                .isInstanceOf(LocalTimeResolver.AmbiguousTimeException.class)
                .satisfies(e -> assertThat(((LocalTimeResolver.AmbiguousTimeException) e).gap).isFalse());
    }

    @Test
    @DisplayName("southern-hemisphere gap resolves the same way (Sydney autumn transition)")
    void sydneyAutumnOverlap() {
        var rules = ZoneId.of("Australia/Sydney").getRules();
        // 2026-04-05 02:30 local is the repeated hour (03:00 AEDT -> 02:00 AEST).
        var local = LocalDateTime.parse("2026-04-05T02:30");
        var earlier = LocalTimeResolver.resolve(local, rules, GapStrategy.LATER, OverlapStrategy.EARLIER)
                .orElseThrow();
        var later = LocalTimeResolver.resolve(local, rules, GapStrategy.LATER, OverlapStrategy.LATER)
                .orElseThrow();
        assertThat(earlier.instant().toString()).isEqualTo("2026-04-04T15:30:00Z");
        assertThat(later.instant().toString()).isEqualTo("2026-04-04T16:30:00Z");
    }
}
