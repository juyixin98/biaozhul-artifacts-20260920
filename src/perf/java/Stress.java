import java.net.URI;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import orderedevents.json.*;
import orderedevents.service.*;
import orderedevents.web.*;

public class Stress {
    public static void main(String[] a) throws Exception {
        ServerConfig cfg = new ServerConfig(0, 100, 8, 2, 10, 2000, 60000, 5, 30000);
        EventService svc = new EventService(cfg);
        ApiServer srv = new ApiServer(cfg, svc);
        srv.start();
        String base = "http://localhost:" + srv.boundPort();
        HttpClient http = HttpClient.newHttpClient();
        int N = 100;
        List<String> ids = new ArrayList<>();
        Random rnd = new Random(42);
        // Submit all N events with random delays; some fail, all maxAttempts=1.
        for (int i = 0; i < N; i++) {
            int delay = 20 + rnd.nextInt(120);
            String beh = (i % 17 == 0) ? "fail" : "succeed";
            String body = "{\"payload\":\"p"+i+"\",\"maxAttempts\":1,\"attempts\":[{\"delayMillis\":"
                + delay + ",\"behavior\":\""+beh+"\"}]}";
            var req = HttpRequest.newBuilder(URI.create(base+"/partitions/stress/events"))
                .header("Content-Type","application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8)).build();
            var resp = http.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
            if (resp.statusCode() != 202) throw new AssertionError("submit "+i+" -> "+resp.statusCode()+" "+resp.body());
            ids.add((String)((Map<?,?>)Json.parse(resp.body())).get("id"));
        }
        // Long-poll for the watermark.
        var req = HttpRequest.newBuilder(URI.create(base+"/partitions/stress/results?afterSeq=-1&waitForSeq=99&waitMillis=15000")).build();
        var resp = http.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        Map<?,?> m = (Map<?,?>) Json.parse(resp.body());
        List<?> results = (List<?>) m.get("results");
        boolean ok = true;
        long prev = -1;
        for (Object o : results) {
            Map<?,?> e = (Map<?,?>) o;
            long seq = ((Number)e.get("seq")).longValue();
            if (seq != prev + 1) { System.out.println("SEQ GAP/JUMP at "+seq+" after "+prev); ok=false; }
            String expectedId = ids.get((int)seq);
            if (!expectedId.equals(e.get("id"))) { System.out.println("ID MISMATCH at seq "+seq); ok=false; }
            String st = (String)e.get("status");
            String expectStatus = (seq % 17 == 0) ? "FAILED" : "SUCCEEDED";
            if (!expectStatus.equals(st)) { System.out.println("STATUS mismatch seq="+seq+" "+st); ok=false; }
            prev = seq;
        }
        System.out.println("committed count=" + results.size() + " expected=" + N);
        System.out.println(ok && results.size() == N ? "STRESS PASS: exactly-once, gapless, per-seq id/status correct" : "STRESS FAIL");
        if (!ok || results.size() != N) System.exit(1);
        srv.stop(); svc.shutdown();
    }
}
