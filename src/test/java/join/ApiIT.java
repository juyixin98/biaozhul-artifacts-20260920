package join;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.Random;

/**
 * End-to-end tests through the real HTTP stack: submit, poll, download result,
 * validation errors, path-traversal rejection, and result correctness.
 * Picks a free ephemeral port itself.
 */
public class ApiIT {

    private int passed;
    private int failed;
    private final List<String> failures = new ArrayList<>();
    private HttpClient http;
    private String base;
    private ApiServer server;
    private Path dataDir;

    public static void main(String[] args) throws Exception {
        new ApiIT().runAll();
    }

    private void runAll() throws Exception {
        dataDir = Files.createTempDirectory("join-api-data-");
        server = new ApiServer(0, dataDir, 2);
        server.start();
        base = "http://localhost:" + server.boundPort();
        http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();

        try {
            health();
            validationErrors();
            pathTraversalRejected();
            endToEndJoin();
            endToEndSpillHotKey();
            cancelViaHttp();
        } finally {
            server.stop();
        }

        System.out.println();
        System.out.println("HTTP tests: " + passed + " passed, " + failed + " failed");
        if (failed > 0) {
            for (String f : failures) {
                System.out.println("FAIL: " + f);
            }
            System.exit(1);
        }
    }

    private void check(boolean cond, String name) {
        if (cond) {
            passed++;
            System.out.println("  ok   " + name);
        } else {
            failed++;
            failures.add(name);
            System.out.println("  FAIL " + name);
        }
    }

    // ------------------------------------------------------------ helpers

    private HttpResponse<String> post(String path, String json) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json, StandardCharsets.UTF_8))
                .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private HttpResponse<String> get(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path)).GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private Map<String, Object> obj(HttpResponse<String> r) {
        return Json.parseObject(r.body());
    }

    private JobView waitFor(String id, long timeoutMs) throws Exception {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < deadline) {
            HttpResponse<String> r = get("/join/" + id);
            Map<String, Object> o = obj(r);
            String st = (String) o.get("status");
            if ("COMPLETED".equals(st) || "FAILED".equals(st) || "CANCELLED".equals(st)) {
                return new JobView(st, o, r.statusCode());
            }
            Thread.sleep(20);
        }
        throw new AssertionError("job " + id + " did not finish within " + timeoutMs + "ms");
    }

    static final class JobView {
        final String status;
        final Map<String, Object> body;
        final int httpStatus;

        JobView(String status, Map<String, Object> body, int httpStatus) {
            this.status = status;
            this.body = body;
            this.httpStatus = httpStatus;
        }
    }

    private String submit(String json) throws Exception {
        HttpResponse<String> r = post("/join", json);
        if (r.statusCode() != 202) {
            throw new AssertionError("submit failed: HTTP " + r.statusCode() + " " + r.body());
        }
        return (String) obj(r).get("id");
    }

    // ---------------------------------------------------------------- tests

    private void health() throws Exception {
        HttpResponse<String> r = get("/health");
        check(r.statusCode() == 200 && r.body().contains("\"ok\""), "GET /health -> 200 ok");
    }

    private void validationErrors() throws Exception {
        HttpResponse<String> r1 = post("/join", "{not json");
        check(r1.statusCode() == 400 && r1.body().contains("BadRequest"),
                "malformed JSON body -> 400");
        HttpResponse<String> r2 = post("/join",
                "{\"leftInput\":\"nope.jsonl\",\"rightInput\":\"x\",\"leftKeyColumn\":\"k\","
                        + "\"rightKeyColumn\":\"k\"}");
        check(r2.statusCode() == 400 && r2.body().contains("does not exist"),
                "missing input file -> 400 with clear message");
        HttpResponse<String> r3 = post("/join",
                "{\"leftInput\":\"l\",\"rightInput\":\"r\",\"leftKeyColumn\":\"k\","
                        + "\"rightKeyColumn\":\"k\",\"memoryBudgetBytes\":0}");
        check(r3.statusCode() == 400, "memoryBudgetBytes=0 -> 400");

        // unknown job
        HttpResponse<String> r4 = get("/join/job-does-not-exist");
        check(r4.statusCode() == 404, "unknown job -> 404");
    }

    private void pathTraversalRejected() throws Exception {
        Path outside = Files.createTempFile("join-outside-", ".jsonl");
        Files.writeString(outside, "{\"k\":1}\n");
        HttpResponse<String> r = post("/join",
                "{\"leftInput\":\"" + outside + "\",\"rightInput\":\"" + outside + "\","
                        + "\"leftKeyColumn\":\"k\",\"rightKeyColumn\":\"k\"}");
        check(r.statusCode() == 400 && r.body().contains("inside the data directory"),
                "absolute path outside data dir -> 400");
        HttpResponse<String> r2 = post("/join",
                "{\"leftInput\":\"../../../../etc/passwd\",\"rightInput\":\"x\","
                        + "\"leftKeyColumn\":\"k\",\"rightKeyColumn\":\"k\"}");
        check(r2.statusCode() == 400, "relative path traversal -> 400");
    }

    private void endToEndJoin() throws Exception {
        Files.createDirectories(dataDir.resolve("inputs"));
        List<String> l = Arrays.asList(
                "{\"k\":1,\"v\":\"a\"}", "{\"k\":1,\"v\":\"b\"}",
                "{\"k\":2,\"v\":\"c\"}", "{\"k\":null,\"v\":\"n\"}");
        List<String> r = Arrays.asList(
                "{\"k\":1,\"v\":\"x\"}", "{\"k\":1,\"v\":\"y\"}",
                "{\"k\":3,\"v\":\"z\"}", "{\"k\":null,\"v\":\"m\"}");
        Files.write(dataDir.resolve("inputs/l.jsonl"), l, StandardCharsets.UTF_8);
        Files.write(dataDir.resolve("inputs/r.jsonl"), r, StandardCharsets.UTF_8);

        String id = submit("{\"leftInput\":\"inputs/l.jsonl\",\"rightInput\":\"inputs/r.jsonl\","
                + "\"leftKeyColumn\":\"k\",\"rightKeyColumn\":\"k\",\"memoryBudgetBytes\":1048576}");
        JobView jv = waitFor(id, 10_000);
        check("COMPLETED".equals(jv.status), "small job COMPLETED (got " + jv.status + ": " + jv.body + ")");
        Map<String, Object> sort = (Map<String, Object>) jv.body.get("sort");
        Map<String, Object> leftInfo = (Map<String, Object>) sort.get("left");
        check(Boolean.FALSE.equals(leftInfo.get("spilled")),
                "status reports no spill for in-memory job");
        check(Long.valueOf(4L).equals(leftInfo.get("totalRows"))
                        && Long.valueOf(1L).equals(leftInfo.get("nullRowsDropped")),
                "row/null counters reported");

        HttpResponse<String> res = get("/join/" + id + "/result");
        check(res.statusCode() == 200, "result download -> 200");
        List<String> lines = nonBlank(res.body().split("\\n"));
        check(lines.size() == 4, "result has 4 cartesian rows for key 1 (got " + lines.size() + ")");
        for (String line : lines) {
            Map<String, Object> o = Json.parseObject(line);
            Map<String, Object> L = (Map<String, Object>) o.get("left");
            Map<String, Object> R = (Map<String, Object>) o.get("right");
            check(Long.valueOf(1L).equals(L.get("k")) && Long.valueOf(1L).equals(R.get("k")),
                    "result row structure {left,right} with matching keys");
        }
        // no duplicates beyond the reference multiset
        check(Collections.frequency(lines,
                "{\"left\":{\"k\":1,\"v\":\"a\"},\"right\":{\"k\":1,\"v\":\"x\"}}") == 1,
                "exact multiset pair (a,x) once");
    }

    private void endToEndSpillHotKey() throws Exception {
        Files.createDirectories(dataDir.resolve("big"));
        Random rnd = new Random(4242);
        int nl = 3000, nr = 2500;
        List<String> l = new ArrayList<>(nl);
        List<String> r = new ArrayList<>(nr);
        for (int i = 0; i < nl; i++) {
            l.add("{\"k\":" + (i % 50) + ",\"i\":" + i + ",\"p\":\"" + filler(40, i) + "\"}");
        }
        for (int i = 0; i < nr; i++) {
            r.add("{\"k\":" + (i % 50) + ",\"i\":" + i + ",\"p\":\"" + filler(40, i + 7) + "\"}");
        }
        Files.write(dataDir.resolve("big/l.jsonl"), l, StandardCharsets.UTF_8);
        Files.write(dataDir.resolve("big/r.jsonl"), r, StandardCharsets.UTF_8);

        // expected: each key occurs 60 x 50 times (3000/50 per side), 50 keys
        long expectedRows = 0;
        for (int k = 0; k < 50; k++) {
            long cl = (nl + 49 - k) / 50; // not exact for shifted ranges, recompute below
        }
        long[] cl = new long[50];
        long[] cr = new long[50];
        for (int i = 0; i < nl; i++) cl[i % 50]++;
        for (int i = 0; i < nr; i++) cr[i % 50]++;
        for (int k = 0; k < 50; k++) expectedRows += cl[k] * cr[k];

        String id = submit("{\"leftInput\":\"big/l.jsonl\",\"rightInput\":\"big/r.jsonl\","
                + "\"leftKeyColumn\":\"k\",\"rightKeyColumn\":\"k\",\"memoryBudgetBytes\":8192}");
        JobView jv = waitFor(id, 60_000);
        check("COMPLETED".equals(jv.status),
                "spill/hot-key job COMPLETED (got " + jv.status + ": " + jv.body + ")");
        Map<String, Object> sort = (Map<String, Object>) jv.body.get("sort");
        check(Boolean.TRUE.equals(((Map<String, Object>) sort.get("left")).get("spilled"))
                        && Boolean.TRUE.equals(((Map<String, Object>) sort.get("right")).get("spilled")),
                "status reports both tables spilled");
        Map<String, Object> joinInfo = (Map<String, Object>) jv.body.get("join");
        check(Long.valueOf(joinInfo.get("groupSpillCount").toString()) > 0
                        || Long.valueOf(((Map<?, ?>) sort.get("left")).get("initialRunCount").toString()) > 1,
                "spill counters present (groupSpillCount=" + joinInfo.get("groupSpillCount") + ")");
        check(Long.parseLong(jv.body.get("outputRows").toString()) == expectedRows,
                "outputRows == expected multiset cardinality " + expectedRows
                        + " (got " + jv.body.get("outputRows") + ")");

        HttpResponse<Path> download = http.send(
                HttpRequest.newBuilder(URI.create(base + "/join/" + id + "/result")).build(),
                HttpResponse.BodyHandlers.ofFile(Files.createTempFile("join-result-", ".jsonl")));
        check(download.statusCode() == 200 && Files.size(download.body()) > 0,
                "result file streamed to disk");

        // per-job temp dir must be gone after completion
        Path tmpJob = dataDir.resolve("_tmp").resolve(id);
        check(waitForTempCleanup(tmpJob, 10_000), "temp dir deleted after COMPLETED");

        // verify exact multiset via independent counting
        Map<String, Long> expected = EngineTestsSupport.referenceMultiset(
                Files.readAllLines(dataDir.resolve("big/l.jsonl")),
                Files.readAllLines(dataDir.resolve("big/r.jsonl")));
        Map<String, Long> actual = EngineTestsSupport.multisetFromFile(download.body());
        check(expected.equals(actual), "downloaded result multiset equals brute-force reference");
    }

    private void cancelViaHttp() throws Exception {
        Files.createDirectories(dataDir.resolve("cancel"));
        Random rnd = new Random(99);
        List<String> rows = new ArrayList<>();
        for (int i = 0; i < 200_000; i++) {
            rows.add("{\"k\":" + rnd.nextInt(1000) + ",\"i\":" + i + ",\"p\":\"" + filler(50, i) + "\"}");
        }
        Files.write(dataDir.resolve("cancel/l.jsonl"), rows, StandardCharsets.UTF_8);
        Files.write(dataDir.resolve("cancel/r.jsonl"), rows, StandardCharsets.UTF_8);

        HttpResponse<String> sr = post("/join",
                "{\"leftInput\":\"cancel/l.jsonl\",\"rightInput\":\"cancel/r.jsonl\","
                        + "\"leftKeyColumn\":\"k\",\"rightKeyColumn\":\"k\",\"memoryBudgetBytes\":4096}");
        check(sr.statusCode() == 202, "heavy job accepted");
        String id = (String) obj(sr).get("id");
        Thread.sleep(100);
        HttpResponse<String> cr = post("/join/" + id + "/cancel", "");
        check(cr.statusCode() == 202, "POST cancel -> 202");
        JobView jv = waitFor(id, 20_000);
        check("CANCELLED".equals(jv.status), "job ends CANCELLED (got " + jv.status + ")");
        Map<String, Object> err = (Map<String, Object>) jv.body.get("error");
        check(err != null && ((String) err.get("message")).contains("cancel"),
                "error message retained and readable via GET /join/{id}: " + err);

        HttpResponse<String> gone = get("/join/" + id + "/result");
        check(gone.statusCode() == 410, "result endpoint -> 410 after cancellation");
        Path tmpJob = dataDir.resolve("_tmp").resolve(id);
        check(waitForTempCleanup(tmpJob, 10_000), "temp files cleaned after HTTP cancellation");
        check(!Files.exists(dataDir.resolve("results").resolve(id + ".jsonl")),
                "partial result file removed after cancellation");

        // double cancel is a clean 409
        HttpResponse<String> again = post("/join/" + id + "/cancel", "");
        check(again.statusCode() == 409, "second cancel -> 409");
    }

    /** The worker deletes the temp dir in its finally block after marking terminal. */
    private static boolean waitForTempCleanup(Path dir, long timeoutMs) throws InterruptedException {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < deadline) {
            if (!Files.exists(dir)) {
                return true;
            }
            Thread.sleep(25);
        }
        return !Files.exists(dir);
    }

    private static List<String> nonBlank(String[] rows) {        List<String> out = new ArrayList<>();
        for (String s : rows) {
            if (!s.isBlank()) {
                out.add(s);
            }
        }
        return out;
    }

    private static String filler(int n, int salt) {
        StringBuilder sb = new StringBuilder(n);
        String a = "abcdefghijklmnopqrstuvwxyz";
        for (int i = 0; i < n; i++) {
            sb.append(a.charAt((i * 7 + salt) % 26));
        }
        return sb.toString();
    }
}
