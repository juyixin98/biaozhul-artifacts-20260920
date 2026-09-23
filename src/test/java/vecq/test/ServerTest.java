package vecq.test;

import vecq.Catalog;
import vecq.Json;
import vecq.QueryEngine;
import vecq.Table;
import vecq.VecqServer;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;
import java.util.Map;

/**
 * 真实启动 {@link VecqServer}（随机端口），通过 HTTP 走完整 JSON 链路：
 * /health、/tables、/query 成功、/query 400 错误形态。
 */
public final class ServerTest {

    public static void run() {
        Catalog catalog = new Catalog();
        catalog.register(Data.orders());
        VecqServer server = new VecqServer(new QueryEngine(catalog));
        try {
            server.start(0);
            int port = server.port();
            String base = "http://localhost:" + port;
            HttpClient client = HttpClient.newHttpClient();

            // health
            HttpResponse<String> h = get(client, base + "/health");
            Assert.eqInt(200, h.statusCode(), "GET /health 200");
            Assert.eq(Boolean.TRUE, Json.asMap(Json.parse(h.body())).get("ok"),
                    "/health ok=true");

            // tables
            HttpResponse<String> tl = get(client, base + "/tables");
            Map<String, Object> tablesResp = Json.asMap(Json.parse(tl.body()));
            List<?> tables = (List<?>) tablesResp.get("tables");
            Assert.eqInt(1, tables.size(), "目录中有 1 张表");

            // query：引用目录中的表名
            String body = "{\"table\":\"orders\",\"batchSize\":2,"
                    + "\"filter\":{\"op\":\"isNull\",\"column\":\"id\"},"
                    + "\"projection\":[\"status\"]}";
            HttpResponse<String> q = post(client, base + "/query", body);
            Assert.eqInt(200, q.statusCode(), "POST /query 200");
            Map<String, Object> qr = Json.asMap(Json.parse(q.body()));
            Assert.eq(List.of(3), qr.get("selectedRows"), "HTTP 查询命中行 3");
            Assert.eq(Boolean.TRUE,
                    ((Map<?, ?>) qr.get("execution")).get("enginesAgree"),
                    "HTTP 响应中双引擎一致");

            // 400：不存在的表
            HttpResponse<String> badTable = post(client, base + "/query",
                    "{\"table\":\"nope\"}");
            Assert.eqInt(400, badTable.statusCode(), "未知表 400");
            Assert.eq(Boolean.FALSE, Json.asMap(Json.parse(badTable.body())).get("ok"),
                    "400 响应 ok=false");

            // 400：无效选择下标
            HttpResponse<String> badSel = post(client, base + "/query",
                    "{\"table\":\"orders\",\"selection\":[42]}");
            Assert.eqInt(400, badSel.statusCode(), "越界 selection 400");

            // 400：JSON 语法错误
            HttpResponse<String> badJson = post(client, base + "/query", "{not json");
            Assert.eqInt(400, badJson.statusCode(), "畸形 JSON 400");

            // GET /query 405
            HttpResponse<String> method = get(client, base + "/query");
            Assert.eqInt(405, method.statusCode(), "GET /query 405");
        } catch (IOException e) {
            Assert.that(false, "HTTP 测试 IO 异常: " + e.getMessage());
        } finally {
            server.stop();
        }
    }

    private static HttpResponse<String> get(HttpClient c, String url) throws IOException {
        try {
            return c.send(HttpRequest.newBuilder(URI.create(url)).GET().build(),
                    HttpResponse.BodyHandlers.ofString());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IOException(e);
        }
    }

    private static HttpResponse<String> post(HttpClient c, String url, String body) throws IOException {
        try {
            return c.send(HttpRequest.newBuilder(URI.create(url))
                            .header("Content-Type", "application/json")
                            .POST(HttpRequest.BodyPublishers.ofString(body)).build(),
                    HttpResponse.BodyHandlers.ofString());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IOException(e);
        }
    }
}
