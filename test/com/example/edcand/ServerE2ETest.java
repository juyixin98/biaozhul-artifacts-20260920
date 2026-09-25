package com.example.edcand;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.List;
import java.util.Map;

/** 端到端：启动真实 HTTP 服务（随机端口），走 HTTP 调用全部接口并校验语义。 */
public final class ServerE2ETest {

    private ServerE2ETest() {
    }

    @SuppressWarnings("unchecked")
    public static void register(TestRunner r) {
        r.add("server: health/stats/distance/search over HTTP, incl. recall", t -> {
            try {
            SearchServer server = SearchServer.withDefaultCorpus();
            server.start(0);
            int port = server.getPort();
            try {
                HttpClient http = HttpClient.newBuilder()
                        .connectTimeout(Duration.ofSeconds(5)).build();
                String base = "http://127.0.0.1:" + port;

                // health
                HttpResponse<String> health = http.send(
                        HttpRequest.newBuilder(URI.create(base + "/health")).GET().build(),
                        HttpResponse.BodyHandlers.ofString());
                t.eq(health.statusCode(), 200, "health 200");
                t.check(health.body().contains("\"ok\""), "health body");

                // stats
                HttpResponse<String> stats = http.send(
                        HttpRequest.newBuilder(URI.create(base + "/stats")).GET().build(),
                        HttpResponse.BodyHandlers.ofString());
                t.eq(stats.statusCode(), 200, "stats 200");
                Map<String, Object> statsMap = Json.parseObject(stats.body());
                t.eq(statsMap.get("unit"), "unicode_codepoint", "unit label");
                t.eq(statsMap.get("q"), ((long) CandidateIndex.Q), "q reported");

                // distance: NFD vs NFC must collapse to 0
                HttpResponse<String> dist = post(http, base + "/distance",
                        "{\"a\":\"cafe\\u0301\",\"b\":\"café\"}");
                t.eq(dist.statusCode(), 200, "distance 200");
                Map<String, Object> distMap = Json.parseObject(dist.body());
                t.eq(distMap.get("distance"), 0L, "NFC-normalized distance 0");

                // distance: emoji is 1 code point
                HttpResponse<String> distEmoji = post(http, base + "/distance",
                        "{\"a\":\"a\",\"b\":\"😀\"}");
                Map<String, Object> em = Json.parseObject(distEmoji.body());
                t.eq(em.get("distance"), 1L, "a/emoji code point distance 1");
                t.eq(((List<Long>) em.get("codePointLengths")), List.of(1L, 1L),
                        "both strings are 1 code point");

                // search recall: peple k=1 contains people d=1
                HttpResponse<String> srch = post(http, base + "/search",
                        "{\"query\":\"peple\",\"threshold\":1}");
                t.eq(srch.statusCode(), 200, "search 200");
                Map<String, Object> sm = Json.parseObject(srch.body());
                List<Map<String, Object>> matches = (List<Map<String, Object>>) sm.get("matches");
                boolean people = matches.stream().anyMatch(m ->
                        "people".equals(m.get("term")) && ((Number) m.get("distance")).intValue() == 1);
                t.check(people, "people must be recalled at distance 1");
                // 没有假阳性：所有返回距离都 <=1
                for (Map<String, Object> m : matches) {
                    t.check(((Number) m.get("distance")).intValue() <= 1,
                            "no false positive beyond threshold");
                }
                // 筛选确实减少了精算量
                int total = ((Number) sm.get("totalTerms")).intValue();
                int exact = ((Number) sm.get("exactLevenshteinCalls")).intValue();
                t.check(exact <= total, "exact calls bounded by total terms");

                // 空查询 k=1
                HttpResponse<String> empty = post(http, base + "/search",
                        "{\"query\":\"\",\"threshold\":1}");
                Map<String, Object> eMap = Json.parseObject(empty.body());
                List<?> eMatches = (List<?>) eMap.get("matches");
                t.check(eMatches.stream().anyMatch(o -> {
                    Map<?, ?> mm = (Map<?, ?>) o;
                    return "".equals(mm.get("term")) && ((Number) mm.get("distance")).intValue() == 0;
                }), "empty query matches empty term");

                // NFKC 让全角串命中
                HttpResponse<String> fw = post(http, base + "/search",
                        "{\"query\":\"abc\",\"threshold\":1,\"normalization\":\"NFKC\"}");
                Map<String, Object> fwMap = Json.parseObject(fw.body());
                List<Map<String, Object>> fwMatches =
                        (List<Map<String, Object>>) fwMap.get("matches");
                t.check(fwMatches.stream().anyMatch(m -> "abc".equals(m.get("term"))),
                        "NFKC folds full-width abc into abc");

                // 错误请求：缺字段 -> 400；坏 JSON -> 400；坏枚举 -> 400
                t.eq(post(http, base + "/search", "{}").statusCode(), 400, "missing fields 400");
                t.eq(post(http, base + "/search", "not json").statusCode(), 400, "bad json 400");
                t.eq(post(http, base + "/search",
                        "{\"query\":\"a\",\"threshold\":-1}").statusCode(), 400, "negative k 400");
                t.eq(post(http, base + "/distance",
                        "{\"a\":\"x\",\"b\":\"y\",\"normalization\":\"WAT\"}").statusCode(),
                        400, "bad normalization 400");
            } finally {
                server.stop();
            }
            } catch (Exception e) {
                throw new RuntimeException(e);
            }
        });
    }

    private static HttpResponse<String> post(HttpClient http, String url, String body)
            throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url))
                .header("Content-Type", "application/json; charset=utf-8")
                .POST(HttpRequest.BodyPublishers.ofString(body, java.nio.charset.StandardCharsets.UTF_8))
                .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
