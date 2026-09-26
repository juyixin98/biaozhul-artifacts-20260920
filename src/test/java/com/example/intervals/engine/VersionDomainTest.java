package com.example.intervals.engine;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class VersionDomainTest {

    private final VersionDomain domain = new VersionDomain();

    @Test
    void usesFixedLexicographicOrder() {
        // Lexicographic, deliberately NOT semantic-version ordering.
        assertTrue(domain.parseEndpoint("v09").compareTo(domain.parseEndpoint("v10")) < 0);
        assertTrue(domain.parseEndpoint("v10").compareTo(domain.parseEndpoint("v9")) < 0);
        assertEquals("v10", domain.formatEndpoint(domain.parseEndpoint("v10")));
    }

    @Test
    void roundTripsArbitraryNonEmptyTokens() {
        for (String token : List.of("a", "v1.2.3", "2024.01", "Ω", "release-candidate")) {
            assertEquals(token, domain.formatEndpoint(domain.parseEndpoint(token)));
        }
    }
}
