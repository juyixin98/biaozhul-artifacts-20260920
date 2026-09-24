package com.tvl.test;

import com.tvl.http.QueryHttpServer;
import com.tvl.json.Json;
import com.tvl.json.JsonWriter;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 端到端 HTTP 测试：进程内启动真实 {@link QueryHttpServer}，
 * 通过 java.net.http（JDK 自带）打请求，验证状态码、三值明细、错误分类。
 */
final class HttpServerTest {

    private HttpServerTest() {
    }

    static void register(TestRunner runner) {
        runner.add("HTTP: /health 与 /query 全链路（三值/跨批次/对照）", a -> {
            QueryHttpServer server = null;
            try {
                server = new QueryHttpServer(0);
                server.start();
                int port = server.getPort();
                HttpClient client = HttpClient.newBuilder()
                        .connectTimeout(Duration.ofSeconds(5)).build();

                // health
                HttpResponse<String> health = client.send(
                        HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + "/health"))
                                .GET().build(),
                        HttpResponse.BodyHandlers.ofString());
                a.eq(health.statusCode(), 200, "/health 200");
                a.check(health.body().contains("\"ok\":true"), "健康响应体");

                // query：两个批次 + 一个参数，跨批 + NULL 位图 + crossCheck
                Map<String, Object> req = new LinkedHashMap<>();
                req.put("sql", "SELECT a, s AS name FROM t WHERE (a >= ? OR a IS NULL)"
                        + " AND s IS NOT NULL");
                req.put("paramTypes", List.of("INTEGER"));
                req.put("params", List.of(10));
                req.put("crossCheck", true);
                Map<String, Object> schemaCol1 = Map.of("name", "a", "type", "INTEGER");
                Map<String, Object> schemaCol2 = Map.of("name", "s", "type", "STRING");
                Map<String, Object> batch1 = Map.of(
                        "schema", List.of(schemaCol1, schemaCol2),
                        "rows", List.of(
                                Map.of("a", 10, "s", "x"),
                                mapOf("a", null, "s", "y"),
                                mapOf("a", 1, "s", null)));
                Map<String, Object> batch2 = Map.of(
                        "schema", List.of(schemaCol1, schemaCol2),
                        "rows", List.of(
                                Map.of("a", 20, "s", "z"),
                                mapOf("a", 5, "s", "w")));
                req.put("batches", List.of(batch1, batch2));

                HttpResponse<String> resp = post(client, port, JsonWriter.write(req));
                a.eq(resp.statusCode(), 200, "正常查询 200");
                Map<?, ?> body = (Map<?, ?>) Json.parse(resp.body());
                a.eq(body.get("ok"), Boolean.TRUE, "ok=true");
                a.eq(body.get("totalSelectedRows"), 3L, "命中 3 行（10、NULL、20；s=NULL 被过滤）");

                List<?> batchResults = (List<?>) body.get("batches");
                a.eq(batchResults.size(), 2, "两个输出批次");
                Map<?, ?> b0 = (Map<?, ?>) batchResults.get(0);
                a.eq(b0.get("truth"), List.of("TRUE", "TRUE", "FALSE"),
                        "批次1 三值明细");
                a.eq(b0.get("crossCheck"), "MATCH", "批次1 向量/逐行 MATCH");
                Map<?, ?> b1 = (Map<?, ?>) batchResults.get(1);
                a.eq(b1.get("crossCheck"), "MATCH", "批次2 向量/逐行 MATCH");
                a.eq(b1.get("selectedRows"), 1L, "批次2 只命中 a=20");

                // 输出列别名
                List<?> outSchema = (List<?>) body.get("outputSchema");
                a.eq(((Map<?, ?>) outSchema.get(1)).get("name"), "name", "别名 name 生效");
            } catch (Exception e) {
                a.fail("HTTP 测试异常: " + e);
            } finally {
                if (server != null) {
                    server.stop();
                }
            }
        });

        runner.add("HTTP: 空批次 rows=[] 与缺失列按 NULL 处理", a -> {
            withServer(a, (client, port) -> {
                Map<String, Object> req = new LinkedHashMap<>();
                req.put("sql", "SELECT a FROM t WHERE a IS NULL");
                req.put("paramTypes", List.of());
                req.put("params", List.of());
                Map<String, Object> schema = Map.of(
                        "schema", List.of(Map.of("name", "a", "type", "INTEGER")),
                        "rows", List.of());
                req.put("batches", List.of(schema));
                HttpResponse<String> resp = post(client, port, JsonWriter.write(req));
                a.eq(resp.statusCode(), 200, "空批次 200");
                Map<?, ?> body = (Map<?, ?>) Json.parse(resp.body());
                Map<?, ?> only = (Map<?, ?>) ((List<?>) body.get("batches")).get(0);
                a.eq(only.get("inputRows"), 0L, "0 输入行");
                a.eq(only.get("rows"), List.of(), "0 输出行");
            });
        });

        runner.add("HTTP: 参数类型错误 -> 422 TYPE_ERROR（拒绝字符串数值混转）", a -> {
            withServer(a, (client, port) -> {
                Map<String, Object> req = baseIntRequest();
                req.put("sql", "SELECT a FROM t WHERE a = ?");
                req.put("paramTypes", List.of("INTEGER"));
                req.put("params", List.of("1")); // 字符串而非数字
                HttpResponse<String> resp = post(client, port, JsonWriter.write(req));
                a.eq(resp.statusCode(), 422, "类型错误应 422");
                Map<?, ?> body = (Map<?, ?>) Json.parse(resp.body());
                a.eq(body.get("errorCode"), "TYPE_ERROR", "错误码 TYPE_ERROR");
                a.check(String.valueOf(body.get("error")).contains("INTEGER"),
                        "错误消息指出期望 INTEGER: " + body.get("error"));
            });
        });

        runner.add("HTTP: 行值类型错误 -> 422；SQL 语法错误 -> 400；坏 JSON -> 400", a -> {
            withServer(a, (client, port) -> {
                // 行值类型：INTEGER 列收到字符串
                Map<String, Object> bad = new LinkedHashMap<>();
                bad.put("sql", "SELECT a FROM t");
                bad.put("paramTypes", List.of());
                bad.put("params", List.of());
                bad.put("batches", List.of(Map.of(
                        "schema", List.of(Map.of("name", "a", "type", "INTEGER")),
                        "rows", List.of(Map.of("a", "not-a-number")))));
                HttpResponse<String> r1 = post(client, port, JsonWriter.write(bad));
                a.eq(r1.statusCode(), 422, "行字符串进 INTEGER 列应 422");
                a.eq(((Map<?, ?>) Json.parse(r1.body())).get("errorCode"), "TYPE_ERROR",
                        "行类型错误码");

                // SQL 语法错误
                Map<String, Object> req2 = baseIntRequest();
                req2.put("sql", "SELECT a FROM t WHERE");
                HttpResponse<String> r2 = post(client, port, JsonWriter.write(req2));
                a.eq(r2.statusCode(), 400, "SQL 语法错误 400");
                a.eq(((Map<?, ?>) Json.parse(r2.body())).get("errorCode"), "SQL_PARSE_ERROR",
                        "SQL 错误码");

                // 坏 JSON
                HttpResponse<String> r3 = post(client, port, "{not json");
                a.eq(r3.statusCode(), 400, "坏 JSON 400");
                a.eq(((Map<?, ?>) Json.parse(r3.body())).get("errorCode"), "INVALID_JSON",
                        "JSON 错误码");

                // 方法不允许
                HttpResponse<String> r4 = client.send(
                        HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + "/query"))
                                .GET().build(),
                        HttpResponse.BodyHandlers.ofString());
                a.eq(r4.statusCode(), 405, "GET /query 405");
            });
        });

        runner.add("HTTP: 跨族比较（STRING 列 vs 数字参数）-> 422", a -> {
            withServer(a, (client, port) -> {
                Map<String, Object> req = new LinkedHashMap<>();
                req.put("sql", "SELECT a FROM t WHERE s > ?");
                req.put("paramTypes", List.of("INTEGER"));
                req.put("params", List.of(3));
                req.put("batches", List.of(Map.of(
                        "schema", List.of(
                                Map.of("name", "a", "type", "INTEGER"),
                                Map.of("name", "s", "type", "STRING")),
                        "rows", List.of(Map.of("a", 1, "s", "x")))));
                HttpResponse<String> resp = post(client, port, JsonWriter.write(req));
                a.eq(resp.statusCode(), 422, "STRING > INTEGER 参数应 422");
            });
        });
    }

    @FunctionalInterface
    interface ServerBlock {
        void run(HttpClient client, int port) throws Exception;
    }

    private static void withServer(Assertions a, ServerBlock block) {
        QueryHttpServer server = null;
        try {
            server = new QueryHttpServer(0);
            server.start();
            HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
            block.run(client, server.getPort());
        } catch (Exception e) {
            a.fail("HTTP 用例异常: " + e);
        } finally {
            if (server != null) {
                server.stop();
            }
        }
    }

    private static HttpResponse<String> post(HttpClient client, int port, String body)
            throws Exception {
        return client.send(
                HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + "/query"))
                        .header("Content-Type", "application/json")
                        .POST(HttpRequest.BodyPublishers.ofString(body))
                        .build(),
                HttpResponse.BodyHandlers.ofString());
    }

    private static Map<String, Object> baseIntRequest() {
        Map<String, Object> req = new LinkedHashMap<>();
        req.put("sql", "SELECT a FROM t");
        req.put("paramTypes", List.of());
        req.put("params", List.of());
        req.put("batches", List.of(Map.of(
                "schema", List.of(Map.of("name", "a", "type", "INTEGER")),
                "rows", List.of(Map.of("a", 1), Map.of("a", 2)))));
        return req;
    }

    private static Map<String, Object> mapOf(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            m.put((String) kv[i], kv[i + 1]);
        }
        return m;
    }
}
