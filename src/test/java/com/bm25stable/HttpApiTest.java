package com.bm25stable;

import com.bm25stable.TestFramework.TestCase;

import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static com.bm25stable.TestFramework.assertEquals;
import static com.bm25stable.TestFramework.assertFalse;
import static com.bm25stable.TestFramework.assertTrue;

/** HTTP 端到端测试：在随机端口启动真实服务，用 JDK HttpClient 调用。 */
public final class HttpApiTest {

    private static HttpClient client;
    private static HttpSearchServer server;
    private static String baseUrl;

    /** 无 @TestCase 注解，由 TestMain 显式调用以控制启停。 */
    public static void setUp() throws Exception {
        SearchEngine engine = new SearchEngine();
        engine.upsertAll(SyntheticCorpus.documents());
        server = new HttpSearchServer(engine, 0);
        server.start();
        baseUrl = "http://localhost:" + server.port();
        client = HttpClient.newHttpClient();
    }

    public static void tearDown() {
        server.stop();
    }

    private record Response(int status, Map<String, Object> json) {
        @SuppressWarnings("unchecked")
        Map<String, Object> error() {
            return (Map<String, Object>) json.get("error");
        }

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> hits() {
            return (List<Map<String, Object>>) json.get("hits");
        }
    }

    private static Response request(String method, String path, String body) throws Exception {
        HttpRequest.Builder builder = HttpRequest.newBuilder(URI.create(baseUrl + path));
        if (body == null) {
            builder.method(method, HttpRequest.BodyPublishers.noBody());
        } else {
            builder.header("Content-Type", "application/json");
            builder.method(method, HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8));
        }
        HttpResponse<String> resp = client.send(builder.build(), HttpResponse.BodyHandlers.ofString());
        Map<String, Object> parsed = Json.parseObject(resp.body());
        return new Response(resp.statusCode(), parsed);
    }

    private static String enc(String s) {
        return URLEncoder.encode(s, StandardCharsets.UTF_8);
    }

    @TestCase
    static void healthCheck() throws Exception {
        Response r = request("GET", "/health", null);
        assertEquals(200, r.status(), "health status");
        assertEquals("ok", r.json().get("status"), "health body");
        assertEquals(21, ((Number) r.json().get("docCount")).intValue(), "synthetic corpus size");
    }

    @TestCase
    static void endToEndPaginationNoGapsNoDuplicates() throws Exception {
        // 合成语料中含 apple 的文档：doc-01, doc-02, doc-03, doc-05, case-1，共 5 篇
        Set<String> collected = new LinkedHashSet<>();
        String cursor = null;
        int pages = 0;
        while (true) {
            String path = "/search?q=" + enc("apple") + "&pageSize=2"
                    + (cursor == null ? "" : "&cursor=" + enc(cursor));
            Response r = request("GET", path, null);
            assertEquals(200, r.status(), "search status");
            for (Map<String, Object> hit : r.hits()) {
                assertFalse(collected.contains(hit.get("docId")),
                        "duplicate id " + hit.get("docId") + " in pagination");
                collected.add((String) hit.get("docId"));
            }
            pages++;
            cursor = (String) r.json().get("nextCursor");
            if (cursor == null) {
                break;
            }
            assertTrue(pages < 10, "pagination terminates");
        }
        assertEquals(5, collected.size(), "all apple docs collected: " + collected);
        assertEquals(Set.of("doc-01", "doc-02", "doc-03", "doc-05", "case-1"),
                collected, "correct hit set");
    }

    @TestCase
    static void snapshotIsolationOverHttp() throws Exception {
        Response page1 = request("GET", "/search?q=" + enc("apple") + "&pageSize=3", null);
        int oldVersion = ((Number) page1.json().get("snapshotVersion")).intValue();
        String cursor = (String) page1.json().get("nextCursor");

        // 批量写入 + 删除，制造新版本
        Response bulk = request("POST", "/documents/bulk",
                "{\"documents\":[{\"id\":\"new-apple-1\",\"text\":\"apple fresh\"},"
                        + "{\"id\":\"new-apple-2\",\"text\":\"apple crisp\"}]}");
        assertEquals(200, bulk.status(), "bulk status");
        Response delete = request("DELETE", "/documents/doc-01", null);
        assertEquals(200, delete.status(), "delete status");
        assertTrue(((Number) delete.json().get("version")).intValue() > oldVersion, "version advanced");

        // 旧游标继续：仍是旧快照
        Response page2 = request("GET",
                "/search?q=" + enc("apple") + "&pageSize=3&cursor=" + enc(cursor), null);
        assertEquals(200, page2.status(), "old cursor still valid");
        assertEquals(oldVersion, ((Number) page2.json().get("snapshotVersion")).intValue(),
                "old snapshot version preserved");

        // 新搜索看到变更后的世界
        Response fresh = request("GET", "/search?q=" + enc("apple") + "&pageSize=20", null);
        List<Object> freshIds = fresh.hits().stream().map(h -> h.get("docId")).toList();
        assertTrue(freshIds.contains("new-apple-1") && freshIds.contains("new-apple-2"),
                "new docs visible to fresh search: " + freshIds);
        assertFalse(freshIds.contains("doc-01"), "deleted doc absent from fresh search");
    }

    @TestCase
    static void expiredCursorReturns410() throws Exception {
        Response page1 = request("GET", "/search?q=" + enc("apple") + "&pageSize=2", null);
        String cursor = (String) page1.json().get("nextCursor");
        assertTrue(cursor != null, "there is a next page");

        for (int i = 0; i < SearchEngine.MAX_RETAINED_SNAPSHOTS; i++) {
            Response upsert = request("POST", "/documents",
                    "{\"id\":\"churn-http-" + i + "\",\"text\":\"churn " + i + "\"}");
            assertEquals(200, upsert.status(), "churn upsert " + i);
        }

        Response stale = request("GET",
                "/search?q=" + enc("apple") + "&pageSize=2&cursor=" + enc(cursor), null);
        assertEquals(410, stale.status(), "expired cursor -> 410");
        assertEquals("SNAPSHOT_EXPIRED", stale.error().get("code"), "error code");
    }

    @TestCase
    static void errorCasesOverHttp() throws Exception {
        // 缺少 q
        Response missingQ = request("GET", "/search?pageSize=3", null);
        assertEquals(400, missingQ.status(), "missing q -> 400");
        assertEquals("BAD_REQUEST", missingQ.error().get("code"), "bad request code");

        // pageSize 越界
        Response badPageSize = request("GET", "/search?q=apple&pageSize=0", null);
        assertEquals(400, badPageSize.status(), "pageSize=0 -> 400");

        // 坏游标
        Response badCursor = request("GET", "/search?q=apple&cursor=%21%21%21", null);
        assertEquals(400, badCursor.status(), "bad cursor -> 400");
        assertEquals("INVALID_CURSOR", badCursor.error().get("code"), "invalid cursor code");

        // 游标与查询不匹配
        Response page1 = request("GET", "/search?q=apple&pageSize=2", null);
        String cursor = (String) page1.json().get("nextCursor");
        Response mismatch = request("GET",
                "/search?q=banana&pageSize=2&cursor=" + enc(cursor), null);
        assertEquals(400, mismatch.status(), "query mismatch -> 400");
        assertEquals("CURSOR_MISMATCH", mismatch.error().get("code"), "mismatch code");

        // 非法 JSON 请求体
        Response badJson = request("POST", "/documents", "{not json");
        assertEquals(400, badJson.status(), "bad json -> 400");

        // 未知路由
        Response notFound = request("GET", "/nope", null);
        assertEquals(404, notFound.status(), "unknown route -> 404");

        // 方法不允许
        Response methodNotAllowed = request("PUT", "/health", null);
        assertEquals(405, methodNotAllowed.status(), "PUT /health -> 405");
    }

    @TestCase
    static void snapshotsListing() throws Exception {
        Response r = request("GET", "/snapshots", null);
        assertEquals(200, r.status(), "snapshots status");
        List<?> list = (List<?>) r.json().get("snapshots");
        assertFalse(list.isEmpty(), "snapshots listed");
        assertEquals(SearchEngine.MAX_RETAINED_SNAPSHOTS,
                ((Number) r.json().get("maxRetained")).intValue(), "max retained exposed");
    }

    @TestCase
    static void queryOnlyPunctuationReturnsEmptyPage() throws Exception {
        Response r = request("GET", "/search?q=" + enc("!!! ???"), null);
        assertEquals(200, r.status(), "punctuation query status");
        assertEquals(0, ((Number) r.json().get("totalHits")).intValue(), "no hits");
        assertEquals(false, r.json().get("hasMore"), "no more");
    }
}
