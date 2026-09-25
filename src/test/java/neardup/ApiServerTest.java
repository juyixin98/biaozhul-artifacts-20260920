package neardup;

import neardup.server.HttpApiServer;
import neardup.server.Json;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;
import java.util.Map;

import static neardup.TestRunner.check;

final class ApiServerTest {

    @SuppressWarnings("unchecked")
    static void run() throws Exception {
        TestRunner.group("http-api");

        HttpApiServer server = new HttpApiServer(0);
        server.start();
        int port = server.boundPort();
        String base = "http://127.0.0.1:" + port;

        HttpClient client = HttpClient.newHttpClient();
        try {
            // GET /health
            HttpResponse<String> health = client.send(
                    HttpRequest.newBuilder(URI.create(base + "/health")).GET().build(),
                    HttpResponse.BodyHandlers.ofString());
            check(health.statusCode() == 200, "GET /health -> 200");
            Map<String, Object> hj = (Map<String, Object>) Json.parse(health.body());
            check("ok".equals(hj.get("status")), "health payload status=ok");
            check(((Number) hj.get("minhashSeed")).longValue() == 20260924L,
                    "health reports the fixed seed");
            check(((Number) hj.get("lshBands")).longValue() == 60L
                            && ((Number) hj.get("lshRows")).longValue() == 2L,
                    "health reports 60x2 LSH");

            // GET /corpus
            HttpResponse<String> corpus = client.send(
                    HttpRequest.newBuilder(URI.create(base + "/corpus")).GET().build(),
                    HttpResponse.BodyHandlers.ofString());
            check(corpus.statusCode() == 200, "GET /corpus -> 200");
            Map<String, Object> cj = (Map<String, Object>) Json.parse(corpus.body());
            check(((Number) cj.get("size")).longValue() == 21L,
                    "corpus size 21 documents", "size=" + cj.get("size"));

            // POST /cluster {} over the synthetic corpus
            HttpResponse<String> clustered = client.send(
                    HttpRequest.newBuilder(URI.create(base + "/cluster"))
                            .header("Content-Type", "application/json")
                            .POST(HttpRequest.BodyPublishers.ofString("{}")).build(),
                    HttpResponse.BodyHandlers.ofString());
            check(clustered.statusCode() == 200, "POST /cluster {} -> 200");
            Map<String, Object> cl = (Map<String, Object>) Json.parse(clustered.body());
            Map<String, Object> stats = (Map<String, Object>) cl.get("stats");
            check(((Number) stats.get("trueEdges")).longValue() == 20L,
                    "API reports 20 true edges");
            check(((Number) stats.get("trueEdgesMissed")).longValue() == 0L,
                    "API reports zero missed edges");
            check(((Number) stats.get("candidateFalsePositives")).longValue() > 0,
                    "API reports false-positive candidates > 0");
            List<Object> clusters = (List<Object>) cl.get("clusters");
            check(clusters.size() == 4, "API returns 4 multi-document clusters",
                    "got " + clusters.size());
            check(((String) cl.get("note")).contains("connected component"),
                    "payload warns that clusters are connected components");

            // The chain cluster must carry the c1-c3 belowThresholdPairs entry.
            boolean foundChainEndpoint = false;
            for (Object o : clusters) {
                Map<String, Object> c = (Map<String, Object>) o;
                if (((Number) c.get("size")).longValue() == 3L) {
                    for (Object bp : (List<Object>) c.get("belowThresholdPairs")) {
                        Map<String, Object> pair = (Map<String, Object>) bp;
                        if ("c1".equals(pair.get("iId")) && "c3".equals(pair.get("jId"))) {
                            foundChainEndpoint = true;
                        }
                    }
                }
            }
            check(foundChainEndpoint, "API exposes (c1,c3) as below-threshold transitive pair");

            // POST /cluster with custom texts
            String custom = "{\"threshold\":0.6,\"texts\":["
                    + "\"same words appear right here now.\","
                    + "\"same words appear right here now.\","
                    + "\"nothing alike within this short text.\"]}";
            HttpResponse<String> cr = client.send(
                    HttpRequest.newBuilder(URI.create(base + "/cluster"))
                            .header("Content-Type", "application/json")
                            .POST(HttpRequest.BodyPublishers.ofString(custom)).build(),
                    HttpResponse.BodyHandlers.ofString());
            check(cr.statusCode() == 200, "POST /cluster custom -> 200");
            Map<String, Object> crj = (Map<String, Object>) Json.parse(cr.body());
            List<Object> cc = (List<Object>) crj.get("clusters");
            check(cc.size() == 1 && ((Number) ((Map<?, ?>) cc.get(0)).get("size")).longValue() == 2,
                    "custom texts produce one size-2 cluster");

            // Bad JSON -> 400
            HttpResponse<String> bad = client.send(
                    HttpRequest.newBuilder(URI.create(base + "/cluster"))
                            .POST(HttpRequest.BodyPublishers.ofString("{not json")).build(),
                    HttpResponse.BodyHandlers.ofString());
            check(bad.statusCode() == 400, "malformed JSON -> 400");

            // Wrong method -> 405
            HttpResponse<String> wrong = client.send(
                    HttpRequest.newBuilder(URI.create(base + "/health")).POST(
                            HttpRequest.BodyPublishers.noBody()).build(),
                    HttpResponse.BodyHandlers.ofString());
            check(wrong.statusCode() == 405, "POST /health -> 405");
        } finally {
            server.stop();
        }
    }
}
