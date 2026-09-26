package com.example.intervals.engine;

import com.example.intervals.fixed.DatasetRepository;
import com.example.intervals.json.ExprDto;
import com.example.intervals.json.IntervalDto;
import com.example.intervals.json.RequestDto;
import com.example.intervals.json.ResponseDto;
import com.example.intervals.tz.TimeZoneInfo;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

class IntervalServiceTest {

    private IntervalService service;

    @BeforeEach
    void setUp() {
        service = new IntervalService(DatasetRepository.loadDefault(), TimeZoneInfo.detect());
    }

    private ExprDto leaf(String name) {
        return new ExprDto(null, name, null, null, null);
    }

    private ExprDto bin(String op, ExprDto l, ExprDto r) {
        return new ExprDto(op, null, null, l, r);
    }

    private ExprDto not(ExprDto arg) {
        return new ExprDto("complement", null, arg, null, null);
    }

    private IntervalDto iv(String lo, boolean loOpen, String hi, boolean hiOpen) {
        return new IntervalDto(lo, loOpen, hi, hiOpen);
    }

    @Test
    void unionIsNormalizedAndSorted() {
        // Overlapping/adjacent windows in shuffled input order.
        Map<String, List<IntervalDto>> sets = Map.of(
                "A", List.of(iv("2024-03-01T00:00:00Z", false, "2024-04-01T00:00:00Z", true)),
                "B", List.of(
                        iv("2024-04-01T00:00:00Z", false, "2024-05-01T00:00:00Z", false),
                        iv("2024-01-01T00:00:00Z", false, "2024-02-01T00:00:00Z", true)));
        RequestDto request = new RequestDto("time", null, sets,
                bin("union", leaf("A"), leaf("B")));

        ResponseDto response = service.process(request);
        assertTrue(response.success());
        // [Jan,Feb) U [Mar,Apr) U [Apr,May] => [Jan,Feb) U [Mar,May]
        assertEquals(2, response.result().intervalCount());
        assertEquals("2024-01-01T00:00:00Z", response.result().intervals().get(0).lower());
        assertEquals("2024-05-01T00:00:00Z", response.result().intervals().get(1).upper());
    }

    @Test
    void differenceRemovesBFromA() {
        Map<String, List<IntervalDto>> sets = Map.of(
                "A", List.of(iv("2024-01-01T00:00:00Z", false, "2024-12-31T00:00:00Z", false)),
                "B", List.of(iv("2024-06-01T00:00:00Z", false, "2024-07-01T00:00:00Z", false)));
        RequestDto request = new RequestDto("time", null, sets,
                bin("difference", leaf("A"), leaf("B")));

        ResponseDto response = service.process(request);
        assertTrue(response.success());
        assertEquals(2, response.result().intervalCount());
        // A-B = [Jan, Jun1) U (Jul1, Dec31]; the second run starts just past B's end.
        assertEquals("2024-07-01T00:00:00Z", response.result().intervals().get(1).lower());
        assertTrue(response.result().intervals().get(1).lowerOpen()); // point excluded
        // The first run ends just before B's start.
        assertEquals("2024-06-01T00:00:00Z", response.result().intervals().get(0).upper());
        assertTrue(response.result().intervals().get(0).upperOpen());
    }

    @Test
    void complementProducesInfiniteBounds() {
        Map<String, List<IntervalDto>> sets = Map.of(
                "A", List.of(iv("2024-01-01T00:00:00Z", false, "2024-12-31T00:00:00Z", false)));
        RequestDto request = new RequestDto("time", null, sets, not(leaf("A")));

        ResponseDto response = service.process(request);
        assertTrue(response.success());
        assertEquals(2, response.result().intervalCount());
        assertNull(response.result().intervals().get(0).lower()); // extends to -inf
        assertNull(response.result().intervals().get(1).upper()); // extends to +inf
    }

    @Test
    void usesFixedDataset() {
        ExprDto expr = bin("intersection", leaf("A"), leaf("B"));
        RequestDto request = new RequestDto("time", "time-adjacency", null, expr);
        ResponseDto response = service.process(request);
        assertTrue(response.success());
        assertEquals("time-adjacency", response.dataset());
        assertNotNull(response.result());
    }

    @Test
    void singletonVersionPointSurvives() {
        Map<String, List<IntervalDto>> sets = Map.of(
                "A", List.of(new IntervalDto("v07", false, "v07", false)),
                "B", List.of(new IntervalDto("v07", false, "v07", false)));
        RequestDto request = new RequestDto("version", null, sets,
                bin("intersection", leaf("A"), leaf("B")));
        ResponseDto response = service.process(request);
        assertTrue(response.success());
        assertEquals(1, response.result().intervalCount());
        IntervalDto point = response.result().intervals().get(0);
        assertEquals("v07", point.lower());
        assertEquals("v07", point.upper());
        assertFalse(point.lowerOpen());
        assertFalse(point.upperOpen());
    }

    @Test
    void responseAlwaysCarriesTimeZoneRecord() {
        Map<String, List<IntervalDto>> sets = Map.of("A", List.of());
        RequestDto request = new RequestDto("time", null, sets, not(leaf("A")));
        ResponseDto response = service.process(request);
        assertNotNull(response.timezone());
        assertTrue(response.timezone().jreTzDataVersion().matches("20\\d{2}[a-z]"));
    }

    // ----- error envelope cases -----

    @Test
    void unknownDomainIsRejected() {
        RequestDto request = new RequestDto("datetime", null,
                Map.of("A", List.of()), leaf("A"));
        ResponseDto response = service.process(request);
        assertFalse(response.success());
        assertEquals("unknown_domain", response.error().code());
    }

    @Test
    void reversedIntervalIsRejected() {
        Map<String, List<IntervalDto>> sets = Map.of(
                "A", List.of(iv("2024-12-31T00:00:00Z", false, "2024-01-01T00:00:00Z", false)));
        RequestDto request = new RequestDto("time", null, sets, leaf("A"));
        ResponseDto response = service.process(request);
        assertFalse(response.success());
        assertEquals("reversed_interval", response.error().code());
    }

    @Test
    void unknownSetReferenceIsRejected() {
        RequestDto request = new RequestDto("time", null,
                Map.of("A", List.of()), leaf("Z"));
        ResponseDto response = service.process(request);
        assertFalse(response.success());
        assertEquals("unknown_set_ref", response.error().code());
    }

    @Test
    void datasetDomainMismatchIsRejected() {
        RequestDto request = new RequestDto("version", "time-adjacency", null,
                leaf("A"));
        ResponseDto response = service.process(request);
        assertFalse(response.success());
        assertEquals("unknown_domain", response.error().code());
    }

    @Test
    void emptyResultIsFlagged() {
        // Disjoint closed intervals intersect to nothing.
        Map<String, List<IntervalDto>> sets = Map.of(
                "A", List.of(new IntervalDto("v01", false, "v02", false)),
                "B", List.of(new IntervalDto("v03", false, "v04", false)));
        RequestDto request = new RequestDto("version", null, sets,
                bin("intersection", leaf("A"), leaf("B")));
        ResponseDto response = service.process(request);
        assertTrue(response.success());
        assertTrue(response.result().empty());
        assertEquals(0, response.result().intervalCount());
    }
}
