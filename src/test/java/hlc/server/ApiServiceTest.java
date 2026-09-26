package hlc.server;

import hlc.HLCException;
import hlc.HLCFileStore;
import hlc.LogicalCounterOverflowException;
import hlc.Test;
import hlc.TestRunner;

import java.nio.file.Path;
import java.util.List;
import java.util.Map;

public class ApiServiceTest {

    private ApiService newService() {
        String tmp = System.getProperty("java.io.tmpdir");
        return new ApiService(new HLCFileStore(
                Path.of(tmp, "hlc-test-state-" + System.nanoTime() + ".properties")));
    }

    @Test("send then receive across two nodes stays monotonic")
    void sendReceive() {
        ApiService svc = newService();
        svc.createNode(Map.of("node", "alice", "initialPhysicalMicros", 1000L));
        svc.createNode(Map.of("node", "bob"));

        Map<String, Object> sent = svc.send(Map.of("node", "alice", "physicalMicros", 2000L));
        Object message = sent.get("message");
        Map<String, Object> recv = svc.receive(Map.of(
                "node", "bob", "physicalMicros", 1500L, "message", message));
        TestRunner.assertEquals("2000:1", recv.get("hlc"),
                "bob lags physically; l=max(0,2000,1500)=2000, c=cm(0)+1=1");
    }

    @Test("a physical backwards step via the API is tolerated")
    void regressionViaApi() {
        ApiService svc = newService();
        svc.createNode(Map.of("node", "n"));
        svc.local(Map.of("node", "n", "physicalMicros", 9000L));
        Map<String, Object> after = svc.local(Map.of("node", "n", "physicalMicros", 1000L));
        TestRunner.assertEquals("9000:1", after.get("hlc"), "l holds, c grows on rollback");
    }

    @Test("save and load rebuilds node clocks")
    void persistence() {
        ApiService svc = newService();
        svc.createNode(Map.of("node", "a"));
        svc.local(Map.of("node", "a", "physicalMicros", 100L));
        Map<String, Object> saved = svc.save();
        TestRunner.assertEquals(Boolean.TRUE, saved.get("saved"), "saved");

        ApiService rebuilt = new ApiService(new HLCFileStore(
                Path.of((String) saved.get("path"))));
        Map<String, Object> loaded = rebuilt.load();
        TestRunner.assertEquals(1, ((List<?>) loaded.get("nodes")).size(), "one node loaded");
        Map<String, Object> snap = rebuilt.snapshot(Map.of("node", "a"));
        TestRunner.assertEquals("100:0", snap.get("hlc"), "timestamp restored");
    }

    @Test("overflow surfaces as an explicit error through the service")
    void overflowViaService() {
        ApiService svc = newService();
        svc.restore(Map.of("node", "z", "state",
                Map.of("l", 1000L, "c", LogicalCounterOverflowException.MAX_COUNTER)));
        TestRunner.assertThrows(LogicalCounterOverflowException.class,
                () -> svc.local(Map.of("node", "z", "physicalMicros", 1000L)));
    }

    @Test("unknown node and missing fields are rejected")
    void validation() {
        ApiService svc = newService();
        TestRunner.assertThrows(HLCException.class,
                () -> svc.local(Map.of("node", "ghost", "physicalMicros", 1L)));
        svc.createNode(Map.of("node", "a"));
        TestRunner.assertThrows(HLCException.class, () -> svc.local(Map.of("node", "a")));
        TestRunner.assertThrows(HLCException.class,
                () -> svc.receive(Map.of("node", "a", "physicalMicros", 1L, "message", "bad")));
    }

    @Test("scenario endpoint proves soundness and lists concurrent pairs")
    void scenario() {
        Map<String, Object> resp = ScenarioService.simulate(Map.of(
                "name", "two independent nodes",
                "events", List.of(
                        Map.of("label", "a1", "node", "a", "type", "LOCAL", "physicalMicros", 200L),
                        Map.of("label", "b1", "node", "b", "type", "LOCAL", "physicalMicros", 100L))));
        TestRunner.assertEquals(Boolean.TRUE, resp.get("soundnessHolds"), "sound");
        TestRunner.assertFalse(((List<?>) resp.get("concurrentButTimestampOrdered")).isEmpty(),
                "b1<a1 by timestamp yet concurrent");
    }
}
