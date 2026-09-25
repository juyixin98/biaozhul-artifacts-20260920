package phrase.test;

import phrase.json.Json;
import phrase.server.ApiServer;
import phrase.search.SearchService;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;

/** 通过真实回环 HTTP 请求测试 JSON 服务（端口 0 = 系统分配）。 */
public final class ApiTest {

    public static void register(TestRunner runner) {
        runner.add("api/health-and-corpus", ApiTest::healthAndCorpus);
        runner.add("api/search-repeated-word", ApiTest::searchRepeated);
        runner.add("api/search-cross-field", ApiTest::searchCrossField);
        runner.add("api/search-stopword-analyzer", ApiTest::searchStopword);
        runner.add("api/analyze-shows-positions", ApiTest::analyze);
        runner.add("api/error-400-on-bad-request", ApiTest::badRequest);
        runner.add("api/error-404-and-method", ApiTest::notFoundAndMethod);
        runner.add("api/terms-array-supported", ApiTest::termsArray);
    }

    private static final HttpClient HTTP = HttpClient.newHttpClient();

    private record Resp(int status, Map<?, ?> body) {}

    private static Resp post(ApiServer api, String path, String json) throws Exception {
        HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create("http://127.0.0.1:" + api.port() + path))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json, StandardCharsets.UTF_8))
                .build();
        HttpResponse<String> res = HTTP.send(req, HttpResponse.BodyHandlers.ofString());
        return new Resp(res.statusCode(),
                res.body().isBlank() ? Map.of() : (Map<?, ?>) Json.parse(res.body()));
    }

    private static Resp get(ApiServer api, String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create("http://127.0.0.1:" + api.port() + path))
                .GET().build();
        HttpResponse<String> res = HTTP.send(req, HttpResponse.BodyHandlers.ofString());
        Object parsed = Json.parse(res.body());
        return new Resp(res.statusCode(),
                parsed instanceof Map<?, ?> m ? m : Map.of("value", parsed));
    }

    @SuppressWarnings("unchecked")
    private static List<Map<?, ?>> hits(Map<?, ?> body) {
        return (List<Map<?, ?>>) body.get("hits");
    }

    private static Map<?, ?> hit(List<Map<?, ?>> hits, String docId) {
        return hits.stream().filter(h -> docId.equals(h.get("docId"))).findFirst()
                .orElse(null);
    }

    private static void healthAndCorpus() throws Exception {
        try (ApiServer api = new ApiServer(new SearchService(0, 100), 0)) {
            api.start();
            Asserts.assertEquals(200, get(api, "/health").status(), "health 200");
            Resp corpus = get(api, "/corpus");
            Asserts.assertEquals(200, corpus.status(), "corpus 200");
            List<?> docs = (List<?>) corpus.body().get("documents");
            Asserts.assertEquals(7, docs.size(), "7 synthetic documents");
        }
    }

    private static void searchRepeated() throws Exception {
        try (ApiServer api = new ApiServer(new SearchService(0, 100), 0)) {
            api.start();
            Resp r = post(api, "/search",
                    "{\"query\":\"echo echo\",\"slop\":0,\"analyzer\":\"standard\"}");
            Asserts.assertEquals(200, r.status(), "200");
            Map<?, ?> d1 = hit(hits(r.body), "d1");
            Asserts.assertEquals(3L, ((Number) d1.get("matchCount")).longValue(),
                    "d1 3 adjacent echo pairs over HTTP");
        }
    }

    private static void searchCrossField() throws Exception {
        try (ApiServer api = new ApiServer(new SearchService(0, 100), 0)) {
            api.start();
            Resp r = post(api, "/search",
                    "{\"query\":\"data pipeline\",\"slop\":0}");
            Map<?, ?> d2 = hit(hits(r.body), "d2");
            Asserts.assertTrue(d2 != null, "d2 hit across fields");
            List<?> matches = (List<?>) d2.get("matches");
            Map<?, ?> first = (Map<?, ?>) matches.get(0);
            Asserts.assertEquals(List.of(8L, 9L), first.get("positions"),
                    "positions 8,9 in response");
            Asserts.assertEquals(Boolean.TRUE, first.get("crossField"), "crossField true");
            Asserts.assertEquals(List.of("body", "tags"), first.get("fields"),
                    "per-term fields");
        }
    }

    private static void searchStopword() throws Exception {
        try (ApiServer api = new ApiServer(new SearchService(0, 100), 0)) {
            api.start();
            // "cat mat" slop2 不命中（停用词保留位置：body 内 cat@4 mat@8 差4 -> 需要 slop3）
            Resp no = post(api, "/search",
                    "{\"query\":\"cat mat\",\"slop\":2,\"analyzer\":\"stopword\"}");
            Asserts.assertEquals(0L, ((Number) no.body().get("totalHits")).longValue(),
                    "slop2 too small due to kept stopword positions");
            Resp yes = post(api, "/search",
                    "{\"query\":\"cat mat\",\"slop\":3,\"analyzer\":\"stopword\"}");
            Map<?, ?> d5 = hit(hits(yes.body), "d5");
            Asserts.assertTrue(d5 != null, "slop3 hits d5");
        }
    }

    private static void analyze() throws Exception {
        try (ApiServer api = new ApiServer(new SearchService(0, 100), 0)) {
            api.start();
            Resp r = post(api, "/analyze",
                    "{\"text\":\"the cat sat\",\"analyzer\":\"stopword\"}");
            Asserts.assertEquals(200, r.status(), "200");
            List<?> tokens = (List<?>) r.body().get("tokens");
            Asserts.assertEquals(2, tokens.size(), "the removed; cat/sat remain");
            Map<?, ?> cat = (Map<?, ?>) tokens.get(0);
            Asserts.assertEquals("cat", cat.get("term"), "cat");
            Asserts.assertEquals(2L, ((Number) cat.get("position")).longValue(),
                    "cat still at position 2 (stopword position kept)");
        }
    }

    private static void badRequest() throws Exception {
        try (ApiServer api = new ApiServer(new SearchService(0, 100), 0)) {
            api.start();
            Resp noQuery = post(api, "/search", "{}");
            Asserts.assertEquals(400, noQuery.status(), "missing query -> 400");
            Asserts.assertEquals(Boolean.TRUE, noQuery.body().get("error"), "error flag");

            Resp badSlop = post(api, "/search",
                    "{\"query\":\"a b\",\"slop\":-1}");
            Asserts.assertEquals(400, badSlop.status(), "negative slop -> 400");

            Resp badJson = post(api, "/search", "not json");
            Asserts.assertEquals(400, badJson.status(), "garbage body -> 400");

            Resp badAnalyzer = post(api, "/search",
                    "{\"query\":\"x\",\"analyzer\":\"nope\"}");
            Asserts.assertEquals(400, badAnalyzer.status(), "bad analyzer -> 400");
        }
    }

    private static void notFoundAndMethod() throws Exception {
        try (ApiServer api = new ApiServer(new SearchService(0, 100), 0)) {
            api.start();
            Asserts.assertEquals(404, get(api, "/nope").status(), "unknown path -> 404");
            Resp getSearch = get(api, "/search");
            Asserts.assertEquals(400, getSearch.status(), "GET /search -> 400");
        }
    }

    private static void termsArray() throws Exception {
        try (ApiServer api = new ApiServer(new SearchService(0, 100), 0)) {
            api.start();
            Resp r = post(api, "/search",
                    "{\"terms\":[\"alpha\",\"alpha\",\"beta\"],\"slop\":0}");
            Asserts.assertEquals(200, r.status(), "200 with terms array");
            Asserts.assertEquals(List.of("alpha", "alpha", "beta"),
                    r.body().get("queryTerms"), "echoed normalized terms");
            Map<?, ?> d4 = hit(hits(r.body), "d4");
            Asserts.assertTrue(d4 != null, "d4 hit via terms array");
        }
    }
}
