package com.example.dstexpand.json;

import com.example.dstexpand.engine.DstExpander;
import com.example.dstexpand.model.ExpandRequest;
import com.example.dstexpand.model.ExpandResponse;
import com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * JSON boundary tests: request parsing and response serialization use the
 * exact wire format documented in README and samples/.
 */
class JsonRoundTripTest {

    private static final String SAMPLE_REQUEST = """
            {
              "zoneId": "America/New_York",
              "startDate": "2026-03-07",
              "endDate": "2026-03-09",
              "rules": [ {"time": "02:30", "label": "nightly"} ],
              "gapPolicy": "LATER",
              "overlapPolicy": "EARLIER"
            }
            """;

    @Test
    void requestParsesFromDocumentedWireFormat() throws Exception {
        ExpandRequest req = Json.mapper().readValue(SAMPLE_REQUEST, ExpandRequest.class);
        ExpandResponse r = new DstExpander().expand(req);
        assertEquals(3, r.getOccurrences().size());
        assertEquals("America/New_York", r.getZoneId());
    }

    @Test
    void responseSerializesWithIsoInstantsAndTzdbVersion() throws Exception {
        ExpandRequest req = Json.mapper().readValue(SAMPLE_REQUEST, ExpandRequest.class);
        ExpandResponse r = new DstExpander().expand(req);
        JsonNode json = Json.mapper().readTree(Json.mapper().writeValueAsString(r));

        assertTrue(json.hasNonNull("tzdbVersion"));
        assertTrue(json.get("tzdbVersion").asText().matches("\\d{4}[a-z]"));
        assertEquals("2026-03-08T07:30:00Z", json.get("occurrences").get(1).get("utc").asText());
        assertEquals("GAP_LATER", json.get("occurrences").get(1).get("resolution").asText());
        assertEquals(3, json.get("stats").get("days").asInt());
    }

    @Test
    void unknownRequestFieldIsRejected() {
        String bad = SAMPLE_REQUEST.replace("\"zoneId\"", "\"zone\"");
        assertThrows(Exception.class, () -> Json.mapper().readValue(bad, ExpandRequest.class));
    }

    @Test
    void invalidPolicyValueIsRejected() {
        String bad = SAMPLE_REQUEST.replace("\"LATER\"", "\"WHENEVER\"");
        assertThrows(Exception.class, () -> Json.mapper().readValue(bad, ExpandRequest.class));
    }
}
