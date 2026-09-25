package com.example.seg;

import com.example.seg.dict.Dictionary;
import com.example.seg.dict.DictionaryRegistry;
import com.example.seg.model.Costs;
import com.example.seg.service.Json;
import com.example.seg.service.SegHttpServer;
import com.example.seg.service.SegService;

import java.math.BigDecimal;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * HTTP 端到端测试：在 127.0.0.1 临时端口启动真实服务，经 TCP 发请求验证状态码与 JSON。
 */
public final class ServerTest extends TestCase {

    public static void main(String[] args) throws Exception {
        System.exit(TestCase.run(new ServerTest()));
    }

    private SegHttpServer server;
    private String base;
    private final HttpClient client = HttpClient.newHttpClient();

    private final Dictionary v1 = Dictionary.builder("v1", Costs.ofInt(5))
            .add("研究", "1").add("研究生", "4").add("生命", "1")
            .add("生", "1").add("命", "4")
            .add("结婚", "2").add("尚未", "1.5").add("的", "1").add("和", "1")
            .build();
    private final Dictionary v2 = Dictionary.builder("v2", Costs.ofInt(8))
            .add("研究", "4").add("研究生", "2.5").add("生命", "4")
            .add("生", "2").add("命", "3").build();

    private HttpResponse<String> post(String path, Object body) throws Exception {
        String payload = body == null ? "" : Json.write(body);
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path))
                .header("Content-Type", "application/json; charset=utf-8")
                .POST(HttpRequest.BodyPublishers.ofString(payload, StandardCharsets.UTF_8))
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private HttpResponse<String> get(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path)).GET().build();
        return client.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> json(HttpResponse<String> r) {
        return (Map<String, Object>) Json.parse(r.body());
    }

    @Override
    protected void run() {
        try {
            DictionaryRegistry reg = new DictionaryRegistry();
            reg.register(v1);
            reg.register(v2);
            server = new SegHttpServer(new SegService(reg));
            int port = server.start(0);
            base = "http://127.0.0.1:" + port;
            System.out.println("（测试服务临时端口 " + port + "）");

            testDictionariesEndpoint();
            testSegmentEndpoint();
            testNbestEndpoint();
            testCrosscheckEndpoint();
            testUnknownVersion404();
            testInvalidJson400();
            testBadK400();
            testMethodNotAllowed405();
            testVersionSwitchOverHttp();
        } catch (Exception e) {
            throw new RuntimeException(e);
        } finally {
            if (server != null) {
                server.stop();
            }
        }
    }

    private void testDictionariesEndpoint() {
        test("HTTP GET /api/dictionaries 200 且含 v1/v2", () -> {
            HttpResponse<String> r = get("/api/dictionaries");
            checkEq(r.statusCode(), 200, "状态码");
            check(r.headers().firstValue("Content-Type").orElse("").contains("application/json"),
                    "JSON Content-Type");
            @SuppressWarnings("unchecked")
            List<Map<String, Object>> dicts =
                    (List<Map<String, Object>>) json(r).get("dictionaries");
            checkEq(dicts.size(), 2, "两个版本");
        });
    }

    private void testSegmentEndpoint() {
        test("HTTP POST /api/segment 分词", () -> {
            Map<String, Object> req = new LinkedHashMap<>();
            req.put("dictionary", "v1");
            req.put("text", "研究生生命");
            HttpResponse<String> r = post("/api/segment", req);
            checkEq(r.statusCode(), 200, "200");
            Map<String, Object> body = json(r);
            @SuppressWarnings("unchecked")
            Map<String, Object> best = (Map<String, Object>) body.get("best");
            checkEq(best.get("segmentation"), "研究/生/生命", "切分");
            checkEq(best.get("totalCost"), new BigDecimal("3"), "代价");
        });
    }

    private void testNbestEndpoint() {
        test("HTTP N最佳 k=5", () -> {
            HttpResponse<String> r = post("/api/segment",
                    Map.of("dictionary", "v1", "text", "结婚的", "k", new BigDecimal("5")));
            checkEq(r.statusCode(), 200, "200");
            @SuppressWarnings("unchecked")
            List<Map<String, Object>> nb =
                    (List<Map<String, Object>>) json(r).get("nbest");
            check(nb.size() >= 2, "至少两条路径");
            checkEq(((BigDecimal) nb.get(0).get("rank")).intValueExact(), 1, "rank 字段");
        });
    }

    private void testCrosscheckEndpoint() {
        test("HTTP POST /api/crosscheck 对照一致", () -> {
            HttpResponse<String> r = post("/api/crosscheck",
                    Map.of("dictionary", "v1", "text", "结婚的和", "k", new BigDecimal("10")));
            checkEq(r.statusCode(), 200, "200");
            checkEq(json(r).get("match"), Boolean.TRUE, "match=true");
        });
    }

    private void testUnknownVersion404() {
        test("HTTP 未知版本返回 404 与错误码", () -> {
            HttpResponse<String> r = post("/api/segment",
                    Map.of("dictionary", "xxx", "text", "命"));
            checkEq(r.statusCode(), 404, "404");
            checkEq(json(r).get("error"), "DICT_NOT_FOUND", "错误码");
        });
    }

    private void testInvalidJson400() {
        test("HTTP 非法 JSON 返回 400", () -> {
            HttpRequest req = HttpRequest.newBuilder(URI.create(base + "/api/segment"))
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString("{not-json", StandardCharsets.UTF_8))
                    .build();
            HttpResponse<String> r = client.send(req,
                    HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
            checkEq(r.statusCode(), 400, "400");
            checkEq(json(r).get("error"), "INVALID_JSON", "错误码");
        });
    }

    private void testBadK400() {
        test("HTTP k 越界返回 400", () -> {
            HttpResponse<String> r = post("/api/segment",
                    Map.of("dictionary", "v1", "text", "命", "k", new BigDecimal("0")));
            checkEq(r.statusCode(), 400, "400");
            checkEq(json(r).get("error"), "BAD_K", "错误码");
        });
    }

    private void testMethodNotAllowed405() {
        test("HTTP 错误方法返回 405", () -> {
            HttpRequest req = HttpRequest.newBuilder(URI.create(base + "/api/segment"))
                    .GET().build();
            HttpResponse<String> r = client.send(req,
                    HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
            checkEq(r.statusCode(), 405, "405");
        });
    }

    private void testVersionSwitchOverHttp() {
        test("HTTP 版本切换：同句两版本结果不同", () -> {
            HttpResponse<String> r1 = post("/api/segment",
                    Map.of("dictionary", "v1", "text", "研究生生命"));
            HttpResponse<String> r2 = post("/api/segment",
                    Map.of("dictionary", "v2", "text", "研究生生命"));
            @SuppressWarnings("unchecked")
            String s1 = (String) ((Map<String, Object>) json(r1).get("best")).get("segmentation");
            @SuppressWarnings("unchecked")
            String s2 = (String) ((Map<String, Object>) json(r2).get("best")).get("segmentation");
            checkEq(s1, "研究/生/生命", "v1");
            checkEq(s2, "研究生/生命", "v2");
            check(!s1.equals(s2), "确实切换");
        });
    }
}
