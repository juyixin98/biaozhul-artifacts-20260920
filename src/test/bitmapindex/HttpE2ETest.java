package bitmapindex;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** End-to-end tests over real HTTP using the JDK HttpClient. */
public final class HttpE2ETest {

    @SuppressWarnings("unchecked")
    public static void run(Asserts a) throws Exception {
        a.caseName("http/lifecycle");
        HttpServerMain app = new HttpServerMain();
        app.start("127.0.0.1", 0); // ephemeral port
        int port = app.getPort();
        String base = "http://127.0.0.1:" + port;

        HttpClient client = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(5))
                .build();

        try {
            // health on empty index
            Map<String, Object> health = postGet(client, base + "/health", "GET", null);
            a.eqInt(((Number) health.get("totalRows")).longValue(), 0, "empty health totalRows=0");

            // load
            String loadBody = Json.write(Map.of(
                    "columns", List.of("city", "color"),
                    "rows", List.of(
                            Map.of("city", "Beijing", "color", "red"),
                            Map.of("city", "Beijing", "color", "blue"),
                            Map.of("city", "Shanghai", "color", "red"),
                            Map.of("city", "Shenzhen", "color", "green"))));
            Map<String, Object> load = postGet(client, base + "/load", "POST", loadBody);
            a.eqInt(((Number) load.get("rows")).longValue(), 4, "loaded 4 rows");

            // query: city = Beijing
            Map<String, Object> q1 = postGet(client, base + "/query", "POST",
                    Json.write(Map.of("op", "eq", "column", "city", "value", "Beijing")));
            a.eqInt(((Number) q1.get("count")).longValue(), 2, "Beijing count=2");
            a.eq(((List<?>) q1.get("rowIds")), List.of(0, 1), "Beijing ids [0,1]");

            // query: NOT(city=Beijing) over full live universe
            Map<String, Object> q2 = postGet(client, base + "/query", "POST",
                    Json.write(Map.of("op", "not",
                            "arg", Map.of("op", "eq", "column", "city", "value", "Beijing"))));
            a.eq(((List<?>) q2.get("rowIds")), List.of(2, 3), "NOT Beijing ids [2,3]");

            // compound: city=Beijing AND color=red
            Map<String, Object> q3 = postGet(client, base + "/query", "POST", Json.write(Map.of(
                    "op", "and",
                    "args", List.of(
                            Map.of("op", "eq", "column", "city", "value", "Beijing"),
                            Map.of("op", "eq", "column", "color", "value", "red")))));
            a.eq(((List<?>) q3.get("rowIds")), List.of(0), "Beijing AND red = [0]");

            // delete id 0, then NOT(city=Beijing) must not bring it back
            postGet(client, base + "/delete", "POST",
                    Json.write(Map.of("rowIds", List.of(0))));
            Map<String, Object> q4 = postGet(client, base + "/query", "POST",
                    Json.write(Map.of("op", "not",
                            "arg", Map.of("op", "eq", "column", "city", "value", "Beijing"))));
            a.eq(((List<?>) q4.get("rowIds")), List.of(2, 3), "NOT after delete still [2,3], no id 0");
            a.eqInt(((Number) q4.get("liveRows")).longValue(), 3, "liveRows=3");

            // query city=Beijing after delete -> only id 1 (ids did not shift to 0)
            Map<String, Object> q5 = postGet(client, base + "/query", "POST",
                    Json.write(Map.of("op", "eq", "column", "city", "value", "Beijing")));
            a.eq(((List<?>) q5.get("rowIds")), List.of(1),
                    "Beijing survivors keep id 1 (no renumbering after delete)");

            // NOT of an impossible predicate = all live rows
            Map<String, Object> q6 = postGet(client, base + "/query", "POST",
                    Json.write(Map.of("op", "not",
                            "arg", Map.of("op", "eq", "column", "city", "value", "Nowhere"))));
            a.eq(((List<?>) q6.get("rowIds")), List.of(1, 2, 3),
                    "NOT(impossible) = exactly the 3 live rows");

            // delete by expression
            Map<String, Object> d2 = postGet(client, base + "/delete", "POST",
                    Json.write(Map.of("expr",
                            Map.of("op", "eq", "column", "color", "value", "red"))));
            a.eqInt(((Number) d2.get("deletedCount")).longValue(), 1, "expr delete removes 1 remaining red");
            a.eq(((List<?>) d2.get("deleted")), List.of(2), "expr-deleted id is 2");

            // restore
            postGet(client, base + "/restore", "POST",
                    Json.write(Map.of("rowIds", List.of(0, 2))));
            Map<String, Object> health2 = postGet(client, base + "/health", "GET", null);
            a.eqInt(((Number) health2.get("liveRows")).longValue(), 4, "restored back to 4 live");

            // stats + row fetch
            Map<String, Object> stats = postGet(client, base + "/stats", "GET", null);
            a.eqInt(((Number) stats.get("totalRows")).longValue(), 4, "stats over HTTP totalRows=4");
            a.check(stats.containsKey("indexBytes"), "stats includes indexBytes");

            Map<String, Object> row0 = postGet(client, base + "/row/0", "GET", null);
            a.eq(((Map<?, ?>) row0.get("data")).get("city"), "Beijing", "row/0 content correct");

            // error handling
            HttpResponse<String> bad = raw(client, base + "/query", "POST",
                    Json.write(Map.of("op", "eq", "column", "missing", "value", "x")));
            a.eqInt(bad.statusCode(), 400, "unknown column -> 400");
            Map<?, ?> errBody = (Map<?, ?>) Json.parse(bad.body());
            a.eq(Boolean.TRUE, errBody.get("error"), "error body flagged");

            // empty dataset over HTTP: NOT must be empty
            postGet(client, base + "/load", "POST", Json.write(Map.of(
                    "columns", List.of("city"), "rows", List.of())));
            Map<String, Object> eq = postGet(client, base + "/query", "POST",
                    Json.write(Map.of("op", "not",
                            "arg", Map.of("op", "eq", "column", "city", "value", "X"))));
            a.eqInt(((Number) eq.get("count")).longValue(), 0, "NOT on freshly emptied dataset -> 0 rows");

            // pagination params
            postGet(client, base + "/load", "POST", Json.write(Map.of(
                    "columns", List.of("k"),
                    "rows", List.of(
                            Map.of("k", "a"), Map.of("k", "a"), Map.of("k", "a"),
                            Map.of("k", "b"), Map.of("k", "b")))));
            Map<String, Object> page = postGet(client, base + "/query?limit=1&offset=1", "POST",
                    Json.write(Map.of("op", "eq", "column", "k", "value", "a")));
            a.eq(((List<?>) page.get("rowIds")), List.of(1), "limit=1 offset=1 over 3 hits returns id 1");
            a.eqInt(((Number) page.get("returned")).longValue(), 1, "returned=1");
            a.eq(Boolean.TRUE, page.get("hasMore"), "hasMore=true");

            Map<String, Object> page2 = postGet(client, base + "/query?limit=2&offset=1", "POST",
                    Json.write(Map.of("op", "eq", "column", "k", "value", "a")));
            a.eq(((List<?>) page2.get("rowIds")), List.of(1, 2), "limit=2 offset=1 returns ids [1,2]");
            a.eq(Boolean.FALSE, page2.get("hasMore"), "hasMore=false on last page");

            Map<String, Object> page3 = postGet(client, base + "/query?limit=10&offset=10", "POST",
                    Json.write(Map.of("op", "eq", "column", "k", "value", "a")));
            a.eq(((List<?>) page3.get("rowIds")), List.of(), "offset past end -> empty page");

        } finally {
            app.stop();
        }
    }

    private static Map<String, Object> postGet(HttpClient client, String url, String method, String body)
            throws Exception {
        HttpResponse<String> resp = raw(client, url, method, body);
        if (resp.statusCode() >= 400) {
            throw new RuntimeException("HTTP " + resp.statusCode() + " for " + method + " " + url
                    + ": " + resp.body());
        }
        Object parsed = Json.parse(resp.body());
        @SuppressWarnings("unchecked")
        Map<String, Object> m = (Map<String, Object>) parsed;
        return m;
    }

    private static HttpResponse<String> raw(HttpClient client, String url, String method, String body)
            throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(url))
                .timeout(Duration.ofSeconds(10));
        if ("POST".equals(method)) {
            b.header("Content-Type", "application/json")
             .POST(HttpRequest.BodyPublishers.ofString(body == null ? "{}" : body));
        } else {
            b.GET();
        }
        return client.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }
}
