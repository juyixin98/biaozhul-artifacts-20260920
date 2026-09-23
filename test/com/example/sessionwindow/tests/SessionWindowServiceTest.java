package com.example.sessionwindow.tests;

import com.example.sessionwindow.model.Event;
import com.example.sessionwindow.model.ResultRecord;
import com.example.sessionwindow.service.BatchProcessor;
import com.example.sessionwindow.service.ServiceConfig;
import com.example.sessionwindow.service.SessionWindowService;
import com.example.sessionwindow.time.ManualTimerService;

import java.util.List;
import java.util.Map;

import static com.example.sessionwindow.tests.Assert.assertEquals;
import static com.example.sessionwindow.tests.Assert.assertNotNull;
import static com.example.sessionwindow.tests.Assert.assertThrows;
import static com.example.sessionwindow.tests.Assert.assertTrue;

public class SessionWindowServiceTest {

    private SessionWindowService newService() {
        return new SessionWindowService(new ManualTimerService());
    }

    @Test
    void pipelineLifecycleCreateGetDelete() {
        SessionWindowService service = newService();
        service.createPipeline("p1", new ServiceConfig(10, 5, false, 0));
        assertTrue(service.pipelineIds().contains("p1"), "listed");

        Map<String, Object> status = service.status("p1");
        assertEquals("p1", status.get("pipelineId"), "status id");
        assertNotNull(status.get("config"), "config present");

        service.deletePipeline("p1");
        assertTrue(!service.pipelineIds().contains("p1"), "removed");
        assertThrows(IllegalArgumentException.class, () -> service.status("p1"),
                "deleted pipeline not found");
    }

    @Test
    void duplicatePipelineRejected() {
        SessionWindowService service = newService();
        service.createPipeline("p", ServiceConfig.defaults());
        assertThrows(IllegalArgumentException.class,
                () -> service.createPipeline("p", ServiceConfig.defaults()),
                "duplicate id rejected");
    }

    @Test
    void ingestReturnsDrainedRecordsAndKeepsState() {
        SessionWindowService service = newService();
        service.createPipeline("p", new ServiceConfig(10, 0, false, 0));

        List<ResultRecord> first = service.ingest("p", List.of(
                BatchProcessor.Item.event(Event.of("u1", 1))));
        assertTrue(first.stream().anyMatch(r -> r.type() == ResultRecord.Type.ADD), "ADD returned");

        List<ResultRecord> second = service.ingest("p", List.of(
                BatchProcessor.Item.watermark(20)));
        assertTrue(second.stream().anyMatch(r -> r.type() == ResultRecord.Type.SEALED), "SEALED");
        assertTrue(second.stream().anyMatch(r -> r.type() == ResultRecord.Type.PURGED), "PURGED");

        // Records are drained per request.
        assertEquals(0, service.ingest("p", List.of()).size(), "no new records when idle");
        Map<String, Object> status = service.status("p");
        assertEquals(0, status.get("retainedSessions"), "state cleaned after purge");
    }

    @Test
    void autoWatermarkPipelineAdvancesOnNewMaxTimestamp() {
        SessionWindowService service = newService();
        service.createPipeline("p", new ServiceConfig(10, 0, true, 0));
        service.ingestEvent("p", Event.of("u1", 1));
        service.ingestEvent("p", Event.of("u1", 15));
        // auto watermark = max ts = 15 (outOfOrderness 0): session [1,11] sealed,
        // [15,25] open.
        Map<String, Object> status = service.status("p");
        assertEquals(15L, status.get("watermark"), "watermark tracks max ts");
        assertEquals(1, status.get("retainedSessions"), "only the later session open");
    }

    @Test
    void unknownPipelineIngestRejected() {
        SessionWindowService service = newService();
        assertThrows(IllegalArgumentException.class,
                () -> service.ingestEvent("nope", Event.of("u1", 1)),
                "ingest to missing pipeline rejected");
    }
}
