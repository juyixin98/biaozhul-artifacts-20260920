package com.example.eventorder.engine;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.example.eventorder.TestFixtures;
import com.example.eventorder.model.Conflict;
import com.example.eventorder.model.OrderOptions;
import com.example.eventorder.model.OrderRequest;
import java.util.List;
import org.junit.jupiter.api.Test;

class VersionRuleCheckerTest {

    private static List<Conflict> versionConflictsOf(OrderRequest request) {
        return VersionRuleChecker.check(RequestValidator.validate(request));
    }

    @Test
    void backwardsVersionEdgeIsContradiction() {
        List<Conflict> conflicts = versionConflictsOf(TestFixtures.versionContradiction());
        assertEquals(1, conflicts.size());
        Conflict conflict = conflicts.get(0);
        assertEquals("VERSION_CONTRADICTION", conflict.kind());
        assertEquals(List.of("a", "b"), conflict.chain());
        assertTrue(conflict.detail().contains("2 > 1"));
    }

    @Test
    void ruleDisabledByDefault() {
        OrderRequest request = TestFixtures.request(
                List.of(TestFixtures.versioned("a", 2), TestFixtures.versioned("b", 1)),
                List.of(TestFixtures.dep("a", "b")));
        assertTrue(versionConflictsOf(request).isEmpty());
    }

    @Test
    void forwardVersionEdgeIsFine() {
        OrderRequest request = TestFixtures.request(
                List.of(TestFixtures.versioned("a", 1), TestFixtures.versioned("b", 2)),
                List.of(TestFixtures.dep("a", "b")),
                new OrderOptions(true, null));
        assertTrue(versionConflictsOf(request).isEmpty());
    }

    @Test
    void unversionedEndpointIsExempt() {
        OrderRequest request = TestFixtures.request(
                List.of(TestFixtures.versioned("a", 5), TestFixtures.event("b")),
                List.of(TestFixtures.dep("a", "b")),
                new OrderOptions(true, null));
        assertTrue(versionConflictsOf(request).isEmpty());
    }
}
