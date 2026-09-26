package com.example.hlc;

import com.example.hlc.core.HlcTimestamp;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class HlcTimestampTest {

    private final ObjectMapper mapper = new ObjectMapper();

    @Test
    void ordersLexicographicallyByPhysicalThenLogicalThenNode() {
        HlcTimestamp a = new HlcTimestamp(1000, 0, "n1");
        HlcTimestamp b = new HlcTimestamp(1000, 1, "n1");
        HlcTimestamp c = new HlcTimestamp(1001, 0, "n1");
        HlcTimestamp d = new HlcTimestamp(1000, 0, "n2");

        assertTrue(a.compareTo(b) < 0);
        assertTrue(b.compareTo(c) < 0);
        assertTrue(a.compareTo(d) < 0, "node id breaks ties to keep the order total");
        assertEquals(0, a.compareTo(new HlcTimestamp(1000, 0, "n1")));
    }

    @Test
    void jsonRoundTrip() throws Exception {
        HlcTimestamp ts = new HlcTimestamp(1727000000123L, 42, "node-1");
        String json = mapper.writeValueAsString(ts);
        assertEquals("{\"physicalMillis\":1727000000123,\"logical\":42,\"nodeId\":\"node-1\"}", json);
        assertEquals(ts, mapper.readValue(json, HlcTimestamp.class));
    }

    @Test
    void rejectsNegativeComponents() {
        assertThrows(IllegalArgumentException.class, () -> new HlcTimestamp(-1, 0, "n"));
        assertThrows(IllegalArgumentException.class, () -> new HlcTimestamp(0, -1, "n"));
        assertThrows(NullPointerException.class, () -> new HlcTimestamp(0, 0, null));
    }
}
