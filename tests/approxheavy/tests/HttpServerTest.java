package approxheavy.tests;

import approxheavy.cms.CountMinSketch;
import approxheavy.core.ManualClock;
import approxheavy.core.ManualScheduler;
import approxheavy.json.Json;
import approxheavy.server.ApiServer;
import approxheavy.server.StreamRegistry;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.Map;

/** End-to-end JSON/HTTP tests on the JDK HttpServer with an injectable clock. */
public final class HttpServerTest {
    private static HttpClient client;

    public static void main(String[] args) throws Exception {
        ManualClock clock = new ManualClock(0);
        ManualScheduler scheduler = new ManualScheduler();
        StreamRegistry registry = new StreamRegistry(clock, scheduler);
        ApiServer server = new ApiServer(0, registry, clock);
        server.start();
        client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
        int port = server.port();
        String base = "http://localhost:" + port;

        TestRunner runner = new TestRunner("HttpServerTest");

        runner.add("health endpoint", () -> {
            HttpResponse<String> r = get(base + "/v1/health");
            TestRunner.checkEq(r.statusCode(), 200, "200");
            TestRunner.check(Json.parseObject(r.body()).get("status").equals("ok"), "ok");
        });

        runner.add("create stream then list", () -> {
            HttpResponse<String> r = put(base + "/v1/streams/s1",
                    "{\"width\":16,\"depth\":3,\"seed\":5,\"candidateCapacity\":8,\"windowMillis\":1000}");
            TestRunner.checkEq(r.statusCode(), 201, "created: " + r.body());
            TestRunner.checkEq(Json.parseObject(r.body()).get("width").toString(), "16", "width echo");            HttpResponse<String> list = get(base + "/v1/streams");
            List<Object> streams = Json.list(Json.parseObject(list.body()).get("streams"));
            TestRunner.checkEq(streams.size(), 1, "one stream");
        });

        runner.add("duplicate stream is 409", () -> {
            put(base + "/v1/streams/dup", "{}");
            HttpResponse<String> again = put(base + "/v1/streams/dup", "{}");
            TestRunner.checkEq(again.statusCode(), 409, "conflict");
        });

        runner.add("unknown stream gives 404", () -> {
            TestRunner.checkEq(get(base + "/v1/streams/nope/count?key=x").statusCode(), 404, "404");
        });

        runner.add("ingest single and batch, then query estimate and bound", () -> {
            HttpResponse<String> one = post(base + "/v1/streams/s1/events",
                    "{\"key\":\"alpha\",\"count\":10,\"timestampMillis\":0}");
            TestRunner.checkEq(one.statusCode(), 202, "accepted: " + one.body());
            HttpResponse<String> batch = post(base + "/v1/streams/s1/events",
                    "{\"timestampMillis\":0,\"events\":["
                            + "{\"key\":\"alpha\",\"count\":5},"
                            + "{\"key\":\"beta\",\"count\":3}]}");
            TestRunner.checkEq(batch.statusCode(), 202, "batch");
            HttpResponse<String> q = get(base + "/v1/streams/s1/count?key=alpha");
            Map<String, Object> body = Json.parseObject(q.body());
            TestRunner.checkEq(((Number) body.get("estimatedCount")).longValue(), 15L, "alpha=15");
            TestRunner.check(((Number) body.get("errorUpperBound")).longValue() >= 0, "bound present");
            TestRunner.check(String.valueOf(body.get("guarantee")).contains(">="),
                    "guarantee text is one-sided");
        });

        runner.add("topk lists heavy hitters and states no coverage guarantee", () -> {
            HttpResponse<String> r = get(base + "/v1/streams/s1/topk?k=5");
            Map<String, Object> body = Json.parseObject(r.body());
            List<Object> items = Json.list(body.get("items"));
            TestRunner.check(items.size() >= 1, "at least one item");
            TestRunner.check(String.valueOf(body.get("candidateCoverageGuarantee")).contains("none"),
                    "coverage disclaimer present");
        });

        runner.add("flush seals a window and last-window queries work", () -> {
            TestRunner.checkEq(post(base + "/v1/streams/s1/flush", "").statusCode(), 200, "flush");
            HttpResponse<String> windows = get(base + "/v1/streams/s1/windows");
            List<Object> list = Json.list(Json.parseObject(windows.body()).get("windows"));
            TestRunner.check(list.size() == 1, "one sealed window");
            HttpResponse<String> q = get(base + "/v1/streams/s1/count?key=alpha&window=last");
            TestRunner.checkEq(((Number) Json.parseObject(q.body()).get("estimatedCount")).longValue(),
                    15L, "sealed alpha");
            HttpResponse<String> top = get(base + "/v1/streams/s1/topk?k=5&window=last");
            TestRunner.checkEq(top.statusCode(), 200, "last topk");
        });

        runner.add("batch with late events reports late count", () -> {
            put(base + "/v1/streams/s2",
                    "{\"width\":16,\"depth\":3,\"seed\":5,\"windowMillis\":100}");
            post(base + "/v1/streams/s2/events", "{\"key\":\"a\",\"timestampMillis\":500}");
            HttpResponse<String> r = post(base + "/v1/streams/s2/events",
                    "{\"events\":["
                            + "{\"key\":\"late\",\"timestampMillis\":10},"
                            + "{\"key\":\"ok\",\"timestampMillis\":500}]}");
            Map<String, Object> body = Json.parseObject(r.body());
            TestRunner.checkEq(((Number) body.get("late")).longValue(), 1L, "one late");
            TestRunner.checkEq(((Number) body.get("accepted")).longValue(), 1L, "one accepted");
        });

        runner.add("/v1/merge accepts compatible sketches", () -> {
            CountMinSketch a = new CountMinSketch(16, 3, 5L);
            a.add("k", 4);
            CountMinSketch b = new CountMinSketch(16, 3, 5L);
            b.add("k", 6);
            String payload = "{\"a\":" + a.toJson() + ",\"b\":" + b.toJson() + "}";
            HttpResponse<String> r = post(base + "/v1/merge", payload);
            TestRunner.checkEq(r.statusCode(), 200, "merge ok: " + r.body());
            Map<String, Object> merged = Json.object(Json.parseObject(r.body()).get("merged"));
            TestRunner.checkEq(((Number) merged.get("totalCount")).longValue(), 10L, "summed");
        });

        runner.add("/v1/merge rejects different seed with 409", () -> {
            CountMinSketch a = new CountMinSketch(16, 3, 5L);
            CountMinSketch b = new CountMinSketch(16, 3, 6L);
            HttpResponse<String> r = post(base + "/v1/merge",
                    "{\"a\":" + a.toJson() + ",\"b\":" + b.toJson() + "}");
            TestRunner.checkEq(r.statusCode(), 409, "409 for seed mismatch: " + r.body());
            TestRunner.check(r.body().contains("seed"), "message mentions seed");
        });

        runner.add("/v1/merge rejects different width with 409", () -> {
            CountMinSketch a = new CountMinSketch(16, 3, 5L);
            CountMinSketch b = new CountMinSketch(32, 3, 5L);
            HttpResponse<String> r = post(base + "/v1/merge",
                    "{\"a\":" + a.toJson() + ",\"b\":" + b.toJson() + "}");
            TestRunner.checkEq(r.statusCode(), 409, "409 width: " + r.body());
        });

        runner.add("malformed JSON body is 400, unknown route 404", () -> {
            HttpResponse<String> bad = post(base + "/v1/streams/s1/events", "{not json");
            TestRunner.checkEq(bad.statusCode(), 400, "400 malformed");
            TestRunner.checkEq(get(base + "/v1/nope").statusCode(), 404, "404 route");
        });

        runner.run();
        server.close();
    }

    private static HttpResponse<String> get(String url) throws IOException, InterruptedException {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url)).GET().build();
        return client.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private static HttpResponse<String> post(String url, String body)
            throws IOException, InterruptedException {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private static HttpResponse<String> put(String url, String body)
            throws IOException, InterruptedException {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url))
                .header("Content-Type", "application/json")
                .PUT(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }
}
