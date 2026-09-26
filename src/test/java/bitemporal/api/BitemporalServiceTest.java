package bitemporal.api;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import bitemporal.error.RecordNotFoundException;
import bitemporal.error.ValidationException;
import bitemporal.model.ChangeRequest;
import bitemporal.model.QueryRequest;
import bitemporal.store.BitemporalStore;

import java.time.Instant;
import java.util.List;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

class BitemporalServiceTest {

    private BitemporalService service;

    @BeforeEach
    void setUp() {
        service = new BitemporalService(new BitemporalStore());
    }

    @Test
    void seedTwiceRequiresReset() {
        service.seed();
        assertThrows(ValidationException.class, service::seed);
    }

    @Test
    void historyRequiresRecordId() {
        assertThrows(ValidationException.class, () -> service.history(" "));
    }

    @Test
    void queryRejectsMissingObservationTime() {
        assertThrows(ValidationException.class,
                () -> service.query(new QueryRequest(null, null, Instant.now(), null)));
    }

    @Test
    void emptyBatchIsRejected() {
        assertThrows(ValidationException.class, () -> service.batch(List.of()));
    }

    @Test
    void unknownBatchStepFailsTheOutcomeAndAborts() {
        BatchResult result = service.batch(List.of(
                new BatchStep("nope", null, null, null, null, null, null, null, null)));
        assertEquals(1, result.steps().size());
        assertEquals(false, result.steps().get(0).success());
        assertEquals("VALIDATION_ERROR", result.steps().get(0).error().code());
        assertEquals(0, result.abortedAt());
    }

    @Test
    void recordNotFoundErrorMapsToStableCode() {
        BatchResult result = service.batch(List.of(
                new BatchStep("commit", "t1", Instant.parse("2026-02-01T00:00:00Z"),
                        List.of(new ChangeRequest("revise", "ghost", "x",
                                Instant.parse("2026-01-01T00:00:00Z"),
                                Instant.parse("2026-02-01T00:00:00Z"))),
                        null, null, null, null, null)));
        assertEquals("RECORD_NOT_FOUND", result.steps().get(0).error().code());
    }

    @Test
    void failedStepWithContinueFlagRunsLaterSteps() {
        service.seed();
        BatchResult result = service.batch(List.of(
                new BatchStep("commit", "bad", Instant.parse("2026-03-05T00:00:00Z"),
                        List.of(new ChangeRequest("insert", "emp-1001", "x",
                                Instant.parse("2026-02-01T00:00:00Z"),
                                Instant.parse("2026-05-01T00:00:00Z"))),
                        null, null, null, null, true),
                new BatchStep("info", null, null, null, null, null, null, null, null)));
        assertEquals(2, result.steps().size());
        assertEquals(false, result.steps().get(0).success());
        assertTrue(result.steps().get(1).success());
        assertEquals(null, result.abortedAt());
    }

    @Test
    void reviseUnknownRecordDirectlyThrows() {
        assertThrows(RecordNotFoundException.class,
                () -> service.commit(new bitemporal.model.TransactionRequest(
                        "t", Instant.parse("2026-02-01T00:00:00Z"),
                        List.of(new ChangeRequest("revise", "ghost", "x",
                                Instant.parse("2026-01-01T00:00:00Z"),
                                Instant.parse("2026-02-01T00:00:00Z"))))));
    }
}
