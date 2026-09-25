package com.example.tjoin;

import com.example.tjoin.json.BatchRunner;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Runs every shipped example scenario through the offline BatchRunner and
 * asserts the headline acceptance outcomes, so the examples cannot silently
 * drift from the implementation.
 */
class BatchRunnerScenarioTest {

    private final ObjectMapper mapper = new ObjectMapper();

    private JsonNode run(String file) throws Exception {
        Path p = Path.of("examples", file);
        JsonNode scenario = mapper.readTree(Files.readString(p));
        return mapper.convertValue(new BatchRunner().run(scenario), JsonNode.class);
    }

    @ParameterizedTest
    @ValueSource(strings = {
            "scenario-basic.json",
            "scenario-stalled-side.json",
            "scenario-buffer-cap.json",
            "scenario-watermark-late.json"})
    @DisplayName("all shipped scenarios execute")
    void allScenariosExecute(String file) throws Exception {
        JsonNode result = run(file);
        assertTrue(result.path("ok").asBoolean());
        assertFalse(result.path("steps").isEmpty());
    }

    @Test
    @DisplayName("basic: 3 boundary-correct pairs; redelivery of R2 is DUPLICATE")
    void basicOutcome() throws Exception {
        JsonNode r = run("scenario-basic.json");
        var metrics = r.path("final").path("metrics");
        assertEquals(3, metrics.path("pairsEmitted").asInt());
        assertEquals(1, metrics.path("duplicatesDropped").asInt());
    }

    @Test
    @DisplayName("stalled side: 3 left records retained; pair emitted on recovery; idle flag set")
    void stalledSideOutcome() throws Exception {
        JsonNode r = run("scenario-stalled-side.json");
        var fin = r.path("final");
        assertEquals(3, fin.path("leftBuffered").asInt(),
                "left records must survive the opposite side's stall");
        assertEquals(2, fin.path("metrics").path("pairsEmitted").asInt());
        assertTrue(fin.path("metrics").path("leftIdle").asBoolean());
        assertFalse(fin.path("metrics").path("rightIdle").asBoolean());
    }

    @Test
    @DisplayName("buffer cap: over-cap event rejected, no eviction, buffered record matches")
    void bufferCapOutcome() throws Exception {
        JsonNode r = run("scenario-buffer-cap.json");
        var metrics = r.path("final").path("metrics");
        assertEquals(1, metrics.path("bufferRejected").asInt());
        assertEquals(0, metrics.path("oldestEvicted").asInt());
        assertEquals(1, metrics.path("pairsEmitted").asInt());
    }

    @Test
    @DisplayName("watermark: boundary-exact cleanup (2), boundary match, late drop")
    void watermarkOutcome() throws Exception {
        JsonNode r = run("scenario-watermark-late.json");
        var metrics = r.path("final").path("metrics");
        assertEquals(2, metrics.path("rightStateCleaned").asInt());
        assertEquals(1, metrics.path("pairsEmitted").asInt());
        assertEquals(1, metrics.path("lateDropped").asInt());
    }
}
