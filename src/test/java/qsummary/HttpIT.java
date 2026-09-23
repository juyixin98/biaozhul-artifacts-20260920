package qsummary;

import com.sun.net.httpserver.HttpServer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Map;

/**
 * End-to-end test that boots the real JDK HTTP server on an ephemeral port
 * and exercises every endpoint over the loopback interface, including:
 * create/observe/stream/quantile/rank, snapshot save/restore, shard merge,
 * 422 on incompatible epsilon, 404s, 400s and bulk-shard accuracy.
 */
public final class HttpIT {

    private static String base;
    private static HttpServer server;
    private static final HttpClient client = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(5))
            .build();

    public static void main(String[] args) throws Exception {
        int exitCode;
        try {
            HttpServerMain app = new HttpServerMain();
            server = app.start("127.0.0.1", 0);
            base = "http://127.0.0.1:" + server.getAddress().getPort();

            testHealth();
            testCrud();
            testValidation();
            testAccuracyOverHttp();
            testSnapshotTransfer();
            testMergeShards();
            testRejectIncompatibleMerge();
            System.out.println("HttpIT: ALL PASSED");
            exitCode = 0;
        } catch (Throwable t) {
            t.printStackTrace(System.err);
            exitCode = 1;
        }
        if (server != null) {
            server.stop(0);
        }
        // The JDK HttpClient keeps non-daemon selector threads alive; exit
        // explicitly (Java 17 HttpClient has no close()).
        System.exit(exitCode);
    }

    private static HttpResponse<String> req(String method, String path, String body)
            throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(base + path))
                .timeout(Duration.ofSeconds(20));
        if (body != null) {
            b.header("Content-Type", "application/json");
            b.method(method, HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8));
        } else {
            b.method(method, HttpRequest.BodyPublishers.noBody());
        }
        return client.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }

    private static void expectStatus(HttpResponse<String> r, int status, String tag) {
        if (r.statusCode() != status) {
            throw new AssertionError(tag + ": expected " + status + " got " + r.statusCode()
                    + " body=" + r.body());
        }
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> json(HttpResponse<String> r) {
        return (Map<String, Object>) Json.parse(r.body());
    }

    private static void testHealth() throws Exception {
        expectStatus(req("GET", "/healthz", null), 200, "health");
        HttpResponse<String> r = req("GET", "/v1/summaries", null);
        expectStatus(r, 200, "list empty");
    }

    private static void testCrud() throws Exception {
        expectStatus(req("PUT", "/v1/summaries/s1", Json.write(Map.of("epsilon", 0.02))),
                201, "create s1");
        // Duplicate create rejected.
        expectStatus(req("PUT", "/v1/summaries/s1", Json.write(Map.of("epsilon", 0.02))),
                400, "duplicate create");
        // Bad id rejected.
        expectStatus(req("PUT", "/v1/summaries/bad%20id", "{}"), 400, "bad id");

        expectStatus(req("POST", "/v1/summaries/s1/observations",
                Json.write(Map.of("values", new double[]{1, 2, 2, 3, 3, 3, 4}))),
                200, "add values");
        HttpResponse<String> meta = req("GET", "/v1/summaries/s1", null);
        expectStatus(meta, 200, "get meta");
        Map<String, Object> m = json(meta);
        if (((Number) m.get("count")).longValue() != 7) {
            throw new AssertionError("count should be 7");
        }

        // Streaming text ingest.
        HttpResponse<String> st = req("POST", "/v1/summaries/s1/observations/stream",
                "5\n6 6\n7\n");
        expectStatus(st, 200, "stream ingest");
        if (((Number) json(st).get("count")).longValue() != 11) {
            throw new AssertionError("count should be 11 after stream ingest");
        }

        expectStatus(req("DELETE", "/v1/summaries/s1", null), 200, "delete");
        expectStatus(req("GET", "/v1/summaries/s1", null), 404, "get after delete");
    }

    private static void testValidation() throws Exception {
        // epsilon out of range
        expectStatus(req("PUT", "/v1/summaries/badeps", Json.write(Map.of("epsilon", 0))),
                400, "epsilon 0");
        expectStatus(req("PUT", "/v1/summaries/badeps", Json.write(Map.of("epsilon", 2))),
                400, "epsilon 2");
        // observe on missing summary
        expectStatus(req("POST", "/v1/summaries/nope/observations",
                Json.write(Map.of("values", new double[]{1}))), 404, "observe missing");
        // non-finite value
        req("PUT", "/v1/summaries/nan-test", Json.write(Map.of("epsilon", 0.01)));
        expectStatus(req("POST", "/v1/summaries/nan-test/observations",
                "{\"values\":[1,\"x\",3]}"), 400, "non-number value");
        // malformed JSON
        expectStatus(req("PUT", "/v1/summaries/x", "{not json"), 400, "malformed create body");
        // quantile on empty summary
        req("PUT", "/v1/summaries/empty", Json.write(Map.of("epsilon", 0.01)));
        expectStatus(req("GET", "/v1/summaries/empty/quantile?q=0.5", null),
                409, "quantile empty -> 409");
        // unknown route
        expectStatus(req("GET", "/nope", null), 404, "unknown route");
    }

    private static void testAccuracyOverHttp() throws Exception {
        String body = bulkBody(TestData.uniform(50_000, TestData.SEED), null);
        expectStatus(req("PUT", "/v1/summaries/u1",
                Json.write(Map.of("epsilon", 0.01))), 201, "create u1");
        expectStatus(req("POST", "/v1/summaries/u1/observations", body), 200, "bulk u1");

        HttpResponse<String> r = req("GET", "/v1/summaries/u1/quantile?qs=0.5,0.9,0.99", null);
        expectStatus(r, 200, "quantile multi");
        Map<String, Object> m = json(r);
        double[] oracle = TestData.sorted(TestData.uniform(50_000, TestData.SEED));
        long bound = (long) Math.floor(0.01d * 50_000);
        @SuppressWarnings("unchecked")
        java.util.List<Object> results = (java.util.List<Object>) m.get("results");
        for (Object o : results) {
            @SuppressWarnings("unchecked")
            Map<String, Object> entry = (Map<String, Object>) o;
            double q = ((Number) entry.get("q")).doubleValue();
            double v = ((Number) endpointValue(entry)).doubleValue();
            long target = Math.max(1, (long) Math.ceil(q * 50_000));
            long rankLE = TestData.exactRankLE(oracle, v);
            long err = Math.abs(rankLE - target);
            if (err > bound + 1) {
                throw new AssertionError("HTTP quantile error exceeds bound: q=" + q
                        + " err=" + err + " bound=" + bound);
            }
        }
        HttpResponse<String> rank = req("GET", "/v1/summaries/u1/rank?value=0.5", null);
        expectStatus(rank, 200, "rank");
        Map<String, Object> rm = json(rank);
        long est = ((Number) rm.get("estimatedRank")).longValue();
        long exact = TestData.exactRankLE(oracle, 0.5d);
        if (Math.abs(est - exact) > bound + 1) {
            throw new AssertionError("HTTP rank error " + Math.abs(est - exact)
                    + " exceeds bound " + bound);
        }
    }

    private static Object endpointValue(Map<String, Object> entry) {
        return entry.get("value");
    }

    @SuppressWarnings("unchecked")
    private static double quantileValue(HttpResponse<String> r) {
        Map<String, Object> m = json(r);
        java.util.List<Object> results = (java.util.List<Object>) m.get("results");
        if (results == null || results.isEmpty()) {
            throw new AssertionError("no results in " + r.body());
        }
        return ((Number) ((Map<String, Object>) results.get(0)).get("value")).doubleValue();
    }

    private static String bulkBody(double[] values, Object ignored) {
        StringBuilder sb = new StringBuilder("{\"values\":[");
        for (int i = 0; i < values.length; i++) {
            if (i > 0) sb.append(',');
            sb.append(values[i]);
        }
        sb.append("]}");
        return sb.toString();
    }

    private static void testSnapshotTransfer() throws Exception {
        // Build a summary, export its snapshot, re-import under a new id,
        // and confirm identical quantile answers.
        expectStatus(req("PUT", "/v1/summaries/ex", Json.write(Map.of("epsilon", 0.01))),
                201, "create ex");
        expectStatus(req("POST", "/v1/summaries/ex/observations",
                bulkBody(TestData.pareto(30_000, 1.1d, TestData.SEED + 5), null)),
                200, "fill ex");
        HttpResponse<String> snap = req("GET", "/v1/summaries/ex/snapshot", null);
        expectStatus(snap, 200, "get snapshot");
        String snapshotJson = Json.write(json(snap).get("snapshot"));
        expectStatus(req("PUT", "/v1/summaries/ex-restored/snapshot",
                Json.write(Map.of("snapshot", Json.parse(snapshotJson)))),
                201, "put snapshot");
        HttpResponse<String> a = req("GET", "/v1/summaries/ex/quantile?q=0.95", null);
        HttpResponse<String> b = req("GET", "/v1/summaries/ex-restored/quantile?q=0.95", null);
        double va = quantileValue(a);
        double vb = quantileValue(b);
        if (va != vb) {
            throw new AssertionError("restored snapshot quantile differs: " + va + " vs " + vb);
        }
        // Reject loading a snapshot wrapped with a bogus version.
        Map<String, Object> bad = (Map<String, Object>) Json.parse(snapshotJson);
        bad.put("version", 7);
        int status = req("PUT", "/v1/summaries/ex-bad/snapshot",
                Json.write(Map.of("snapshot", bad))).statusCode();
        if (status != 422 && status != 400) {
            throw new AssertionError("bad snapshot version expected 422/400, got " + status);
        }
    }

    private static void testMergeShards() throws Exception {
        int shards = 4;
        int per = 30_000;
        double[][] all = new double[shards][];
        StringBuilder ids = new StringBuilder();
        for (int k = 0; k < shards; k++) {
            all[k] = TestData.pareto(per, 1.1d, TestData.SEED + 100 + k);
            String id = "sh" + k;
            expectStatus(req("PUT", "/v1/summaries/" + id,
                    Json.write(Map.of("epsilon", 0.01))), 201, "create " + id);
            expectStatus(req("POST", "/v1/summaries/" + id + "/observations",
                    bulkBody(all[k], null)), 200, "fill " + id);
            if (k > 0) ids.append(',');
            ids.append('"').append(id).append('"');
        }
        String mergeBody = "{\"id\":\"merged\",\"sources\":[" + ids + "]}";
        HttpResponse<String> r = req("POST", "/v1/merge", mergeBody);
        expectStatus(r, 201, "merge shards");
        Map<String, Object> m = json(r);
        long count = ((Number) m.get("count")).longValue();
        if (count != (long) shards * per) {
            throw new AssertionError("merged count " + count);
        }

        // Accuracy of the merged summary vs exact union oracle.
        double[] union = TestData.sorted(concatShards(all));
        long bound = (long) Math.floor(0.01d * union.length);
        for (double q : new double[]{0.5, 0.9, 0.99, 0.999, 1.0}) {
            HttpResponse<String> qr = req("GET",
                    "/v1/summaries/merged/quantile?q=" + q, null);
            double v = quantileValue(qr);
            long target = Math.max(1, (long) Math.ceil(q * union.length));
            long rankLE = TestData.exactRankLE(union, v);
            long err = Math.abs(rankLE - target);
            if (err > bound + 1) {
                throw new AssertionError("HTTP merged quantile bound violated q=" + q
                        + " err=" + err + " bound=" + bound);
            }
        }
        System.out.println("HTTP merge accuracy within bound " + bound);
    }

    private static double[] concatShards(double[][] shards) {
        int n = 0;
        for (double[] s : shards) n += s.length;
        double[] out = new double[n];
        int off = 0;
        for (double[] s : shards) {
            System.arraycopy(s, 0, out, off, s.length);
            off += s.length;
        }
        java.util.Arrays.sort(out);
        return out;
    }

    private static void testRejectIncompatibleMerge() throws Exception {
        req("PUT", "/v1/summaries/eps1", Json.write(Map.of("epsilon", 0.01)));
        req("PUT", "/v1/summaries/eps2", Json.write(Map.of("epsilon", 0.02)));
        req("POST", "/v1/summaries/eps1/observations",
                Json.write(Map.of("values", new double[]{1, 2, 3})));
        req("POST", "/v1/summaries/eps2/observations",
                Json.write(Map.of("values", new double[]{4, 5, 6})));
        HttpResponse<String> r = req("POST", "/v1/merge",
                "{\"id\":\"bad-merge\",\"sources\":[\"eps1\",\"eps2\"]}");
        expectStatus(r, 422, "incompatible epsilon merge -> 422");
        System.out.println("HTTP incompatible merge rejected: "
                + Json.parse(r.body()));
        // Merge into an existing target id must fail.
        expectStatus(req("POST", "/v1/merge",
                "{\"id\":\"eps1\",\"sources\":[\"eps1\"]}"), 400, "merge target exists");
    }
}
