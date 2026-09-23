package cdcrebuild.test;

import cdcrebuild.codec.Json;
import cdcrebuild.engine.CdcEngine;
import cdcrebuild.http.ApiServer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Path;
import java.time.Duration;
import java.util.List;
import java.util.Map;

/** 真实 HTTP 端到端测试：启动 JDK HttpServer，用 JDK HttpClient 打端口。 */
public final class HttpTest {

    private HttpTest() {
    }

    public static void run(TestRunner t) {
        Path dir = Events.tempDir("cdc-http-");
        try (CdcEngine engine = CdcEngine.open(dir, "id");
             ApiServer server = ApiServer.start(0, engine)) {
            int port = server.port();
            String base = "http://localhost:" + port;
            HttpClient http = HttpClient.newBuilder()
                    .connectTimeout(Duration.ofSeconds(5)).build();

            try {

                t.test("HTTP: healthz", () -> {
                    HttpResponse<String> r = get(http, base + "/healthz");
                    t.assertEquals(200, r.statusCode(), "healthz 200");
                    t.assertEquals(Boolean.TRUE, Json.parseObject(r.body()).get("ok"), "ok=true");
                });

                t.test("HTTP: 完整提交事务 -> 200 且行可读", () -> {
                    post(http, base + "/v1/events", Events.begin(1, "h1"));
                    post(http, base + "/v1/events",
                            Events.insert(2, "h1", "users", Events.row("id", 1, "name", "ada")));
                    HttpResponse<String> r = post(http, base + "/v1/events", Events.commit(3, "h1"));
                    t.assertEquals(200, r.statusCode(), "COMMIT 返回 200");
                    HttpResponse<String> row = get(http, base + "/v1/tables/users/row/1");
                    t.assertEquals(200, row.statusCode(), "按主键查行 200");
                    t.assertEquals("ada", Json.parseObject(row.body()).get("name"), "行内容正确");
                });

                t.test("HTTP: 未提交数据在快照中不可见", () -> {
                    post(http, base + "/v1/events", Events.begin(4, "h2"));
                    HttpResponse<String> r = post(http, base + "/v1/events",
                            Events.insert(5, "h2", "users", Events.row("id", 2)));
                    t.assertEquals(200, r.statusCode(), "DATA 200（已落 WAL）");
                    Map<String, Object> tables = Json.parseObject(get(http, base + "/v1/tables").body());
                    List<?> rows = (List<?>) tables.get("users");
                    t.assertEquals(1, rows.size(), "未提交行不可见，users 仍为 1 行");
                });

                t.test("HTTP: 回滚 -> 200，审计日志 committed=false", () -> {
                    HttpResponse<String> r = post(http, base + "/v1/events", Events.rollback(6, "h2"));
                    t.assertEquals(200, r.statusCode(), "ROLLBACK 200");
                    Map<String, Object> log = Json.parseObject(
                            get(http, base + "/v1/log?from=1&to=6").body());
                    t.assertNum(2, log.get("count"), "有两条事务记录");
                    List<?> txs = (List<?>) log.get("transactions");
                    Map<?, ?> second = (Map<?, ?>) txs.get(1);
                    t.assertEquals("h2", second.get("txId"), "第二条是 h2");
                    t.assertEquals(Boolean.FALSE, second.get("committed"), "h2 已回滚");
                });

                t.test("HTTP: 乱序缺口 -> 202 BUFFERED + PAUSED，补齐后恢复", () -> {
                    post(http, base + "/v1/events", Events.begin(7, "h3"));
                    HttpResponse<String> buffered = post(http, base + "/v1/events",
                            Events.insert(9, "h3", "users", Events.row("id", 7)));
                    t.assertEquals(202, buffered.statusCode(), "缺口事件返回 202");
                    t.assertEquals("PAUSED", Json.parseObject(buffered.body()).get("state"), "PAUSED");
                    Map<String, Object> st = Json.parseObject(get(http, base + "/v1/status").body());
                    t.assertNum(8, st.get("firstMissingPos"), "firstMissingPos=8");
                    // 补 pos=8（先于 9 的 DATA），pos=9 已缓冲，自动排空
                    HttpResponse<String> fixed = post(http, base + "/v1/events",
                            Events.insert(8, "h3", "users", Events.row("id", 3)));
                    t.assertEquals(200, fixed.statusCode(), "补齐事件 200");
                    t.assertEquals("RUNNING", Json.parseObject(fixed.body()).get("state"), "恢复 RUNNING");
                    post(http, base + "/v1/events", Events.commit(10, "h3"));
                    t.assertNum(3, Json.parseObject(get(http, base + "/v1/tables/users").body())
                            .get("count"), "users 现有 3 行（1,3,7）");
                });

                t.test("HTTP: 重复位置 -> 200 DUPLICATE", () -> {
                    HttpResponse<String> r = post(http, base + "/v1/events", Events.commit(10, "h3"));
                    t.assertEquals(200, r.statusCode(), "重复投递 200");
                    t.assertEquals("DUPLICATE", Json.parseObject(r.body()).get("status"), "DUPLICATE");
                });

                t.test("HTTP: 结构错误 -> 400", () -> {
                    HttpResponse<String> r = postRaw(http, base + "/v1/events", "{not json");
                    t.assertEquals(400, r.statusCode(), "坏 JSON 400");
                    HttpResponse<String> r2 = postRaw(http, base + "/v1/events", "[]");
                    t.assertEquals(400, r2.statusCode(), "非对象 400");
                });

                t.test("HTTP: 语义冲突 -> 409", () -> {
                    HttpResponse<String> r = post(http, base + "/v1/events",
                            Events.insert(11, "no-txn", "users", Events.row("id", 99)));
                    t.assertEquals(409, r.statusCode(), "未 BEGIN 的 DATA 409");
                    t.assertEquals("PAUSED", engine.status().get("state"), "非法事件使消费暂停");
                    // 用同一位置重投合法事件（BEGIN h4），覆盖坏事件并恢复
                    HttpResponse<String> fixed = post(http, base + "/v1/events", Events.begin(11, "h4"));
                    t.assertEquals(200, fixed.statusCode(), "重投修正后 200");
                    t.assertEquals("RUNNING", Json.parseObject(fixed.body()).get("state"), "恢复 RUNNING");
                });

                t.test("HTTP: 未知路由 -> 404，主键变更后旧键 404", () -> {
                    t.assertEquals(404, get(http, base + "/nope").statusCode(), "未知路由 404");
                    post(http, base + "/v1/events", Events.update(12, "h4", "users",
                            Events.row("id", 1, "name", "ada"),
                            Events.row("id", 11, "name", "ada2")));
                    post(http, base + "/v1/events", Events.commit(13, "h4"));
                    t.assertEquals(404, get(http, base + "/v1/tables/users/row/1").statusCode(),
                            "主键变更后旧键 404");
                    t.assertEquals(200, get(http, base + "/v1/tables/users/row/11").statusCode(),
                            "新键 200");
                });
            } finally {
                // HttpClient 在 JDK 21 起才实现 AutoCloseable，此处无需显式释放
            }
        } catch (Exception e) {
            throw new AssertionError("HTTP 测试初始化失败: " + e, e);
        }
    }

    private static HttpResponse<String> get(HttpClient http, String url) {
        try {
            HttpRequest req = HttpRequest.newBuilder(URI.create(url)).GET().build();
            return http.send(req, HttpResponse.BodyHandlers.ofString());
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    private static HttpResponse<String> post(HttpClient http, String url,
                                            cdcrebuild.model.Event event) {
        return postRaw(http, url, Json.write(event.toMap()));
    }

    private static HttpResponse<String> postRaw(HttpClient http, String url, String json) {
        try {
            HttpRequest req = HttpRequest.newBuilder(URI.create(url))
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(json))
                    .build();
            return http.send(req, HttpResponse.BodyHandlers.ofString());
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }
}
