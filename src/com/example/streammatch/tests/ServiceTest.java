package com.example.streammatch.tests;

import com.example.streammatch.json.Json;
import com.example.streammatch.server.JsonHttpService;
import com.sun.net.httpserver.HttpServer;

import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;

import static com.example.streammatch.tests.TestFramework.assertEquals;
import static com.example.streammatch.tests.TestFramework.assertTrue;
import static com.example.streammatch.tests.TestFramework.section;

/**
 * 服务层测试：业务方法直调 + 真实 HTTP 回环（启动在随机空闲端口）。
 * 覆盖 /health、/corpora、/corpus、/match、/match/stream、错误请求、
 * 外部 patternId（含重复模式显式 ID）与码点/字节两种切块模式。
 */
public final class ServiceTest {

    private ServiceTest() {
    }

    public static void run() {
        section("JSON 服务（业务方法 + HTTP 回环）", () -> {
            // ---- 业务方法 ----
            Map<String, Object> resp = JsonHttpService.handleMatch(Map.of(
                    "text", "ushers",
                    "patterns", List.of(
                            Map.of("id", 100, "pattern", "he"),
                            "she",
                            Map.of("id", 100, "pattern", "he"), // 重复显式 ID
                            "hers"),
                    "emptyPolicy", "SKIP"));
            assertEquals(Boolean.TRUE, resp.get("ok"), "handleMatch ok");
            assertEquals(Boolean.TRUE, resp.get("matchesNaive"), "服务端朴素核对通过");
            @SuppressWarnings("unchecked")
            List<Map<String, Object>> matches = (List<Map<String, Object>>) resp.get("matches");
            // she@1, he(id0->100)@2, he(id2->100)@2, hers@2；规范化排序首条为 she@1
            assertEquals(4, matches.size(), "命中数 4");
            assertEquals("she", matches.get(0).get("pattern"), "排序后首条为 she@1");
            assertEquals(1, matches.get(0).get("start"), "she 起点 1");
            long heCount = matches.stream()
                    .filter(x -> x.get("pattern").equals("he")).count();
            assertEquals(2L, heCount, "两个重复 he 各自输出（外部 ID 均为 100 但两条记录）");
            assertTrue(matches.stream().filter(x -> x.get("pattern").equals("he"))
                    .allMatch(x -> ((Number) x.get("patternId")).intValue() == 100),
                    "两个 he 的外部 ID 都是 100");

            // 显式 id 缺失时按下标
            Map<String, Object> auto = JsonHttpService.handleMatch(Map.of(
                    "text", "ab", "patterns", List.of("a", "b")));
            @SuppressWarnings("unchecked")
            List<Map<String, Object>> am = (List<Map<String, Object>>) auto.get("matches");
            assertEquals(0, am.get(0).get("patternId"), "自动 ID 从 0 开始");
            assertEquals(1, am.get(1).get("patternId"), "第二个自动 ID 为 1");

            // 错误请求
            expectBadRequest(() -> JsonHttpService.handleMatch(Map.of("text", "a")), "缺少 patterns");
            expectBadRequest(() -> JsonHttpService.handleMatch(Map.of("patterns", List.of("a"))),
                    "缺少 text/corpusName");
            expectBadRequest(() -> JsonHttpService.handleMatch(Map.of(
                    "text", "a", "patterns", List.of("a"), "emptyPolicy", "WHAT")),
                    "非法 emptyPolicy");
            expectBadRequest(() -> JsonHttpService.handleStream(Map.of(
                    "text", "a", "patterns", List.of("a"), "chunkCodePoints", 0)),
                    "非法 chunkCodePoints");
            expectBadRequest(() -> JsonHttpService.handleStream(Map.of(
                    "text", "a", "patterns", List.of("a"),
                    "byteMode", true, "chunkBytes", 0)), "非法 chunkBytes");
            expectBadRequest(() -> JsonHttpService.handleStream(Map.of(
                    "text", "a", "patterns", List.of("a"), "chunkCodePoints", -2)),
                    "负数 chunkCodePoints");

            // 码点切块流
            Map<String, Object> st = JsonHttpService.handleStream(Map.of(
                    "text", "a😀bushers",
                    "patterns", List.of("he", "she", "hers", "😀"),
                    "chunkCodePoints", 2));
            assertEquals(Boolean.TRUE, st.get("matchesStream"), "码点切块：流式 == 一次性");
            assertEquals(Boolean.TRUE, st.get("matchesNaive"), "码点切块：流式 == 朴素");
            @SuppressWarnings("unchecked")
            List<Map<String, Object>> chunks = (List<Map<String, Object>>) st.get("chunks");
            assertTrue(chunks.size() >= 3, "产生多个分块（含 finish 块）");
            @SuppressWarnings("unchecked")
            Map<String, Object> emojiHit = chunks.stream()
                    .flatMap(x -> ((List<Map<String, Object>>) x.get("hits")).stream())
                    .filter(x -> x.get("pattern").equals("😀")).findFirst().orElse(null);
            assertTrue(emojiHit != null, "emoji 跨越分块后仍命中");
            assertEquals(1, emojiHit.get("start"), "emoji 全局码点位置为 1");

            // 字节切块流（刻意 1 字节一切）
            Map<String, Object> stb = JsonHttpService.handleStream(Map.of(
                    "text", "x😀ushers",
                    "patterns", List.of("he", "she", "hers", "😀"),
                    "byteMode", true, "chunkBytes", 1));
            assertEquals(Boolean.TRUE, stb.get("matchesStream"), "1 字节切块：流式 == 一次性");
            assertEquals(Boolean.TRUE, stb.get("matchesNaive"), "1 字节切块：流式 == 朴素");

            // BEFORE 策略 + 分块：1 个空模式在 n=3 的流上产出 n+1=4 个空命中 + 1 个 a
            Map<String, Object> ste = JsonHttpService.handleStream(Map.of(
                    "text", "abc", "patterns", List.of("", "a"),
                    "emptyPolicy", "BEFORE", "chunkCodePoints", 2));
            assertEquals(Boolean.TRUE, ste.get("matchesNaive"), "BEFORE 流式集合与朴素一致");
            assertEquals(5, ste.get("streamedMatchCount"), "BEFORE: 4 空命中 + 1 实命中");
            // 两个空模式：2*(n+1)=8 + 1 个 a = 9
            Map<String, Object> ste2 = JsonHttpService.handleStream(Map.of(
                    "text", "abc", "patterns", List.of("", "a", ""),
                    "emptyPolicy", "BEFORE", "chunkCodePoints", 1));
            assertEquals(Boolean.TRUE, ste2.get("matchesNaive"), "BEFORE(双空模式) 流式与朴素一致");
            assertEquals(9, ste2.get("streamedMatchCount"), "BEFORE(双空模式): 8 空命中 + 1 实命中");

            // 合成语料入口
            Map<String, Object> cp = JsonHttpService.handleMatch(Map.of(
                    "corpusName", "overlap", "seed", 3,
                    "patterns", List.of("he", "she", "aaaa")));
            assertEquals(Boolean.TRUE, cp.get("matchesNaive"), "语料入口朴素核对通过");
            assertTrue(((Number) cp.get("matchCount")).intValue() > 0, "overlap 语料有命中");

            // ---- 真实 HTTP 回环 ----
            try {
                HttpServer server = JsonHttpService.startServer(0);
                int port = server.getAddress().getPort();
                HttpClient client = HttpClient.newHttpClient();

                HttpResponse<String> health = client.send(
                        HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/health"))
                                .GET().build(), HttpResponse.BodyHandlers.ofString());
                assertEquals(200, health.statusCode(), "GET /health 200");
                assertTrue(health.body().contains("\"ok\":true"), "health 体含 ok:true");

                HttpResponse<String> corpora = client.send(
                        HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/corpora"))
                                .GET().build(), HttpResponse.BodyHandlers.ofString());
                assertEquals(200, corpora.statusCode(), "GET /corpora 200");
                assertTrue(corpora.body().contains("sharedPrefix"), "corpora 列出语料");

                String reqBody = Json.write(Map.of(
                        "text", "ushers", "patterns", List.of("he", "she", "his", "hers")));
                HttpResponse<String> matched = client.send(
                        HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/match"))
                                .header("Content-Type", "application/json")
                                .POST(HttpRequest.BodyPublishers.ofString(reqBody)).build(),
                        HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
                assertEquals(200, matched.statusCode(), "POST /match 200");
                @SuppressWarnings("unchecked")
                Map<String, Object> mj = (Map<String, Object>) Json.parse(matched.body());
                assertEquals(Boolean.TRUE, mj.get("matchesNaive"), "HTTP /match 朴素核对通过");
                assertEquals(3, ((Number) mj.get("matchCount")).intValue(),
                        "HTTP /match 命中 3 个（she/he/hers）");

                // 400：坏 JSON
                HttpResponse<String> bad = client.send(
                        HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/match"))
                                .header("Content-Type", "application/json")
                                .POST(HttpRequest.BodyPublishers.ofString("{bad json")).build(),
                        HttpResponse.BodyHandlers.ofString());
                assertEquals(400, bad.statusCode(), "坏 JSON 返回 400");
                assertTrue(bad.body().contains("\"ok\":false"), "400 体含 ok:false");

                // 405
                HttpResponse<String> method = client.send(
                        HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/health"))
                                .POST(HttpRequest.BodyPublishers.noBody()).build(),
                        HttpResponse.BodyHandlers.ofString());
                assertEquals(405, method.statusCode(), "/health 只接受 GET");

                // /corpus?name=unicode&seed=9
                HttpResponse<String> corpus = client.send(
                        HttpRequest.newBuilder(URI.create(
                                        "http://localhost:" + port + "/corpus?name=unicode&seed=9"))
                                .POST(HttpRequest.BodyPublishers.ofString("{}")).build(),
                        HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
                assertEquals(200, corpus.statusCode(), "POST /corpus?name=unicode 200");
                assertTrue(corpus.body().contains("😀"), "unicode 语料体含 emoji（UTF-8 正常）");

                server.stop(0);
            } catch (Exception e) {
                assertTrue(false, "HTTP 回环异常: " + e);
            }
        });
    }

    private static void expectBadRequest(Runnable r, String label) {
        boolean threw = false;
        try {
            r.run();
        } catch (JsonHttpService.BadRequestException | IllegalArgumentException e) {
            threw = true;
        }
        assertTrue(threw, "错误请求被拒绝: " + label);
    }
}
