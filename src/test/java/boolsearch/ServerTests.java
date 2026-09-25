package boolsearch;

import boolsearch.TestRunner.Case;
import boolsearch.json.Json;
import boolsearch.server.SearchServer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;
import java.util.Map;

/** HTTP 服务集成测试：真实起服务、发请求、校验 JSON 响应。 */
public final class ServerTests {

    static void register(List<Case> cases) {
        cases.add(new Case("server: 端到端（增删查、纯NOT、未知词、错误位置）", ServerTests::endToEnd));
    }

    private static HttpClient client = HttpClient.newHttpClient();
    private static String base;

    private static HttpResponse<String> send(String method, String path, String body) throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(base + path));
        if (body == null) {
            b.method(method, HttpRequest.BodyPublishers.noBody());
        } else {
            b.method(method, HttpRequest.BodyPublishers.ofString(body))
                    .header("Content-Type", "application/json");
        }
        return client.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> json(HttpResponse<String> r) throws Exception {
        return (Map<String, Object>) Json.parse(r.body());
    }

    static void endToEnd() throws Exception {
        SearchServer server = new SearchServer(0); // 随机空闲端口
        server.start();
        base = "http://localhost:" + server.port();
        try {
            // 健康检查
            HttpResponse<String> r = send("GET", "/health", null);
            Check.eq(r.statusCode(), 200, "health 状态码");
            Check.eq(json(r).get("status"), "ok", "health 内容");

            // 录入文档
            Check.eq(send("POST", "/documents", "{\"id\":1,\"text\":\"apple banana\"}").statusCode(),
                    200, "录入 doc1");
            send("POST", "/documents", "{\"id\":2,\"text\":\"banana cherry\"}");
            send("POST", "/documents", "{\"id\":3,\"text\":\"cherry\"}");

            // 普通查询
            r = send("POST", "/query", "{\"query\":\"apple AND NOT cherry\"}");
            Check.eq(json(r).get("docIds"), List.of(1L), "apple AND NOT cherry");

            // 纯 NOT
            r = send("POST", "/query", "{\"query\":\"NOT cherry\"}");
            Check.eq(json(r).get("docIds"), List.of(1L), "纯 NOT 查询");

            // 未知词
            r = send("POST", "/query", "{\"query\":\"nosuchterm\"}");
            Check.eq(json(r).get("docIds"), List.of(), "未知词应为空");
            r = send("POST", "/query", "{\"query\":\"NOT nosuchterm\"}");
            Check.eq(json(r).get("docIds"), List.of(1L, 2L, 3L), "NOT 未知词应为全集");

            // 优化开关结果一致
            r = send("POST", "/query", "{\"query\":\"banana AND NOT apple\",\"optimize\":false}");
            Check.eq(json(r).get("docIds"), List.of(2L), "关闭优化结果一致");

            // 解析错误：位置保留
            r = send("POST", "/query", "{\"query\":\"apple AND\"}");
            Check.eq(r.statusCode(), 400, "解析错误应返回 400");
            Check.eq(json(r).get("position"), 9L, "错误位置应为 9（EOF）");

            r = send("POST", "/query", "{\"query\":\"(apple OR\"}");
            Check.eq(json(r).get("position"), 9L, "缺少右括号位置");

            // 删除文档后全集收缩
            Check.eq(send("DELETE", "/documents/1", null).statusCode(), 200, "删除 doc1");
            r = send("POST", "/query", "{\"query\":\"NOT cherry\"}");
            Check.eq(json(r).get("docIds"), List.of(), "删除后纯 NOT 结果应变空");
            Check.eq(send("DELETE", "/documents/99", null).statusCode(), 404, "删除不存在文档");

            // 文档列表
            r = send("GET", "/documents", null);
            Check.eq(json(r).get("docIds"), List.of(2L, 3L), "当前文档全集");

            // 合成语料重建
            r = send("POST", "/corpus", "{\"docs\":50,\"seed\":7}");
            Check.eq(r.statusCode(), 200, "重建语料");
            Check.eq(json(r).get("docs"), 50L, "语料文档数");
        } finally {
            server.stop();
        }
    }
}
