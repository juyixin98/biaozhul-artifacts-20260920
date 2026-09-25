package com.example.segmenter.tests;

import com.example.segmenter.data.CorpusLoader;
import com.example.segmenter.json.Json;
import com.example.segmenter.model.Dictionary;
import com.example.segmenter.service.SegmentServer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static com.example.segmenter.tests.TestFramework.check;
import static com.example.segmenter.tests.TestFramework.assertEquals;
import static com.example.segmenter.tests.TestFramework.suite;

/** 端到端 HTTP 测试：在随机端口真实启动服务，用 JDK HttpClient 发请求。 */
public final class HttpServerTest {

    private HttpServerTest() {
    }

    @SuppressWarnings("unchecked")
    public static void run() throws Exception {
        suite("http service end-to-end");

        Dictionary v1 = CorpusLoader.load(Path.of("data/corpora/dict_v1.corpus"));
        Dictionary v2 = CorpusLoader.load(Path.of("data/corpora/dict_v2.corpus"));
        Map<String, Dictionary> dictionaries = new LinkedHashMap<>();
        dictionaries.put(v1.version(), v1);
        dictionaries.put(v2.version(), v2);

        SegmentServer server = SegmentServer.start(0, dictionaries, 10.0, "dict_v1");
        int port = server.port();
        String base = "http://127.0.0.1:" + port;
        HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();

        try {
            // GET /healthz
            HttpResponse<String> health = get(client, base + "/healthz");
            assertEquals("healthz 200", 200, health.statusCode());
            check("healthz body", health.body().contains("\"status\" : \"ok\"")
                    || health.body().contains("\"status\": \"ok\""));

            // GET /versions
            HttpResponse<String> versions = get(client, base + "/versions");
            assertEquals("versions 200", 200, versions.statusCode());
            Map<String, Object> vBody = (Map<String, Object>) Json.parse(versions.body());
            assertEquals("默认版本为 dict_v1", "dict_v1", vBody.get("default"));
            List<Object> versionList = (List<Object>) vBody.get("versions");
            assertEquals("列出 2 个版本", 2, versionList.size());

            // POST /segment n=1
            HttpResponse<String> r1 = post(client, base + "/segment",
                    Map.of("text", "研究生命的起源"));
            assertEquals("segment 200", 200, r1.statusCode());
            Map<String, Object> b1 = (Map<String, Object>) Json.parse(r1.body());
            assertEquals("回显版本", "dict_v1", b1.get("version"));
            Map<String, Object> best = (Map<String, Object>) b1.get("best");
            check("best.words 存在", best.get("words") instanceof List);
            check("best.totalCost 是数字", best.get("totalCost") instanceof Number);
            List<Object> tokens = (List<Object>) best.get("tokens");
            check("tokens 非空", !tokens.isEmpty());
            Map<String, Object> firstToken = (Map<String, Object>) tokens.get(0);
            check("token 含 unknown 布尔", firstToken.get("unknown") instanceof Boolean);
            check("token 含 cost 数字", firstToken.get("cost") instanceof Number);

            // POST /segment n=3
            HttpResponse<String> r3 = post(client, base + "/segment",
                    Map.of("text", "研究生生命", "n", 3L));
            assertEquals("n=3 200", 200, r3.statusCode());
            Map<String, Object> b3 = (Map<String, Object>) Json.parse(r3.body());
            List<Object> results = (List<Object>) b3.get("results");
            assertEquals("返回 3 条结果", 3, results.size());
            check("n=3 时不返回 best 别名", !b3.containsKey("best"));
            double c0 = ((Number) ((Map<String, Object>) results.get(0)).get("totalCost")).doubleValue();
            double c1 = ((Number) ((Map<String, Object>) results.get(1)).get("totalCost")).doubleValue();
            check("结果按代价升序", c0 <= c1 + 1e-9);
            assertEquals("rank 从 1 开始", 1,
                    ((Number) ((Map<String, Object>) results.get(0)).get("rank")).intValue());

            // 版本切换：同文本 v1/v2 结果不同
            HttpResponse<String> sv1 = post(client, base + "/segment",
                    Map.of("text", "研究生命的起源", "version", "dict_v1"));
            HttpResponse<String> sv2 = post(client, base + "/segment",
                    Map.of("text", "研究生命的起源", "version", "dict_v2"));
            Object w1 = ((Map<String, Object>) Json.parse(sv1.body())).get("best");
            Object w2 = ((Map<String, Object>) Json.parse(sv2.body())).get("best");
            check("v1/v2 切分结果不同", !w1.equals(w2));

            // 未知字符
            HttpResponse<String> unk = post(client, base + "/segment",
                    Map.of("text", "研究Ψ生命"));
            Map<String, Object> ub = (Map<String, Object>) Json.parse(unk.body());
            Map<String, Object> ubBest = (Map<String, Object>) ub.get("best");
            List<Object> ubWords = (List<Object>) ubBest.get("words");
            assertEquals("未知字保留在词序列", List.of("研究", "Ψ", "生命"), ubWords);
            assertEquals("unknownCount=1", 1,
                    ((Number) ubBest.get("unknownCount")).intValue());

            // 空串
            HttpResponse<String> empty = post(client, base + "/segment",
                    Map.of("text", ""));
            assertEquals("空串 200", 200, empty.statusCode());
            Map<String, Object> eb = (Map<String, Object>) Json.parse(empty.body());
            Map<String, Object> eBest = (Map<String, Object>) eb.get("best");
            assertEquals("空串 words=[]", List.of(), eBest.get("words"));
            assertEquals("空串 totalCost=0", 0,
                    ((Number) eBest.get("totalCost")).intValue());

            // 错误：缺 text
            HttpResponse<String> noText = postRaw(client, base + "/segment", "{}");
            assertEquals("缺 text 返回 400", 400, noText.statusCode());
            check("错误体含 error 字段",
                    ((Map<String, Object>) Json.parse(noText.body())).containsKey("error"));

            // 错误：非法 JSON
            HttpResponse<String> badJson = postRaw(client, base + "/segment", "{not json");
            assertEquals("非法 JSON 返回 400", 400, badJson.statusCode());

            // 错误：未知版本
            HttpResponse<String> badVer = post(client, base + "/segment",
                    Map.of("text", "研究", "version", "no-such"));
            assertEquals("未知版本返回 404", 404, badVer.statusCode());

            // 错误：n 越界 / 非整数
            assertEquals("n=0 返回 400", 400,
                    post(client, base + "/segment", Map.of("text", "研究", "n", 0L)).statusCode());
            assertEquals("n=65 返回 400", 400,
                    post(client, base + "/segment",
                            Map.of("text", "研究", "n", 65L)).statusCode());
            assertEquals("n=1.5 返回 400", 400,
                    post(client, base + "/segment",
                            Map.of("text", "研究", "n", 1.5)).statusCode());

            // 错误：方法不允许
            HttpResponse<String> getSegment = get(client, base + "/segment");
            assertEquals("GET /segment 返回 405", 405, getSegment.statusCode());

            // 文本类型错误
            HttpResponse<String> numText = postRaw(client, base + "/segment",
                    "{\"text\":123}");
            assertEquals("text 非字符串 400", 400, numText.statusCode());

            // 请求体不是 object
            HttpResponse<String> arrBody = postRaw(client, base + "/segment", "[1,2]");
            assertEquals("数组请求体 400", 400, arrBody.statusCode());

            // codePoints 回显
            HttpResponse<String> cp = post(client, base + "/segment",
                    Map.of("text", "研究😀"));
            Map<String, Object> cpBody = (Map<String, Object>) Json.parse(cp.body());
            assertEquals("codePoints 按码点计为 3", 3,
                    ((Number) cpBody.get("codePoints")).intValue());

        } finally {
            server.stop();
        }
    }

    private static HttpResponse<String> get(HttpClient client, String url) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url))
                .timeout(Duration.ofSeconds(10))
                .GET().build();
        return client.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private static HttpResponse<String> post(HttpClient client, String url, Map<String, Object> body)
            throws Exception {
        return postRaw(client, url, Json.write(body));
    }

    private static HttpResponse<String> postRaw(HttpClient client, String url, String raw)
            throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url))
                .timeout(Duration.ofSeconds(10))
                .header("Content-Type", "application/json; charset=utf-8")
                .POST(HttpRequest.BodyPublishers.ofString(raw, StandardCharsets.UTF_8))
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }
}
