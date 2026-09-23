package dedup.tests;

import dedup.json.Json;
import dedup.server.DedupHttpServer;
import dedup.state.FileSnapshotStore;
import dedup.stream.StreamProcessor;
import dedup.time.Clock;
import dedup.time.TaskScheduler;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;

/**
 * 端到端测试：启动真实 HTTP 服务（随机端口 + 文件快照），
 * 通过网络发请求，完成“服务运行 → 关闭落盘 → 新实例重启恢复”的完整链路。
 */
final class HttpEndToEndTest {

    private HttpEndToEndTest() {}

    private static final HttpClient HTTP = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(2))
            .build();

    static void register(TestFramework tf) {
        tf.run("HTTP端到端：事件去重/水位线/极迟标志/outputs/统计 全链路", a -> {
            Path dir = null;
            try {
                dir = Files.createTempDirectory("dedup-http-");
                Path stateFile = dir.resolve("state").resolve("snapshot.json");
                var config = new StreamProcessor.Config(5_000, 1000, "manual", 0, 1000);

                var proc = StreamProcessor.create(config, Clock.system(),
                        new FileSnapshotStore(stateFile));
                var server = new DedupHttpServer(0, proc,
                        TaskScheduler.system(Clock.system()), 60_000);
                server.start();
                int port = server.port();

                // health
                HttpResponse<String> health = get(port, "/health");
                a.eq(health.statusCode(), 200, "/health 200");
                a.check(health.body().contains("\"ok\""), "健康状态 ok");

                // 首次事件
                Json.Obj r1 = post(port, "/events",
                        "{\"id\":\"n1\",\"eventTime\":10000,\"payload\":{\"v\":1}}");
                a.eq(((Json.Obj) r1.get("result")).getStr("decision"), "ACCEPT", "n1 ACCEPT");

                // 重复（载荷不同）-> SUPPRESS + payloadMismatch
                Json.Obj r2 = post(port, "/events",
                        "{\"id\":\"n1\",\"eventTime\":10000,\"payload\":{\"v\":2}}");
                Json.Obj d2 = (Json.Obj) r2.get("result");
                a.eq(d2.getStr("decision"), "SUPPRESS", "n1 重复 SUPPRESS");
                a.eq(d2.get("payloadMismatch"), Json.Bool.TRUE, "payloadMismatch=true");

                // batch：一条新事件 + 一条重复
                Json.Obj batch = post(port, "/events/batch", """
                        {"events":[
                          {"id":"n2","eventTime":10000,"payload":{}},
                          {"id":"n1","eventTime":10000,"payload":{"v":1}}
                        ]}""");
                a.eqLong(((Json.Num) batch.get("count")).value().longValueExact(), 2,
                        "batch count=2");

                // 推进水位线并制造极迟重复
                Json.Obj wm = post(port, "/watermark", "{\"watermark\":15001}");
                a.eq(wm.get("advanced"), Json.Bool.TRUE, "水位线推进");
                Json.Obj late = (Json.Obj) post(port, "/events",
                        "{\"id\":\"n1\",\"eventTime\":10000}").get("result");
                a.eq(late.getStr("decision"), "UNGUARANTEED", "极迟重复 UNGUARANTEED");
                a.eq(late.get("dedupGuaranteed"), Json.Bool.FALSE,
                        "dedupGuaranteed=false 通过 HTTP 可观察");

                // 时钟回退：提交更小水位线
                Json.Obj back = post(port, "/watermark", "{\"watermark\":1}");
                a.eq(back.get("advanced"), Json.Bool.FALSE, "回退被拒绝");

                // outputs：n1(首次) + n2 + n1极迟透传 = 3 条
                Json.Obj outs = getJson(port, "/outputs?drain=false");
                a.eqLong(((Json.Num) outs.get("count")).value().longValueExact(), 3,
                        "输出共 3 条（无承诺范围内重复）");
                long guaranteed = ((Json.Arr) outs.get("outputs")).stream()
                        .map(o -> (Json.Obj) o)
                        .map(o -> (Json.Obj) o.get("decision"))
                        .filter(d -> d.get("dedupGuaranteed") == Json.Bool.TRUE)
                        .count();
                a.eqLong(guaranteed, 2, "承诺范围内仅 n1首条、n2 两条输出");

                // stats
                Json.Obj stats = getJson(port, "/stats");
                a.eqLong(((Json.Num) stats.get("suppressed")).value().longValueExact(), 2,
                        "suppressed=2");
                a.eqLong(((Json.Num) stats.get("unguarded")).value().longValueExact(), 1,
                        "unguarded=1");
                a.eqLong(((Json.Num) stats.get("payloadMismatch")).value().longValueExact(), 1,
                        "payloadMismatch=1");

                // 非法 JSON 返回 400
                HttpResponse<String> bad = postRaw(port, "/events", "{not json");
                a.eq(bad.statusCode(), 400, "非法 JSON 返回 400");
                HttpResponse<String> bad2 = postRaw(port, "/events", "[]");
                a.eq(bad2.statusCode(), 400, "非对象体返回 400");

                // 关闭（落盘）并重启：n2 仍在窗口内（15001 下界 10001，n2@10000 实际已过期）
                server.stop();

                var proc2 = StreamProcessor.create(config, Clock.system(),
                        new FileSnapshotStore(stateFile));
                var server2 = new DedupHttpServer(0, proc2,
                        TaskScheduler.system(Clock.system()), 60_000);
                server2.start();
                try {
                    Json.Obj stats2 = getJson(server2.port(), "/stats");
                    a.eqLong(((Json.Num) stats2.get("suppressed")).value().longValueExact(), 2,
                            "重启后 suppressed 计数延续");
                    // 新事件在当前水位线下（15001）eventTime=10000 属于极迟，先推一个当前事件
                    Json.Obj nowEvent = (Json.Obj) post(server2.port(), "/events",
                            "{\"id\":\"n3\",\"eventTime\":20000}").get("result");
                    a.eq(nowEvent.getStr("decision"), "ACCEPT", "重启后新事件可接受");
                } finally {
                    server2.stop();
                }
            } catch (Exception e) {
                a.fail("端到端异常: " + e);
            } finally {
                if (dir != null) {
                    try (var walk = Files.walk(dir)) {
                        walk.sorted((x, y) -> y.compareTo(x)).forEach(p -> {
                            try { Files.deleteIfExists(p); } catch (Exception ignored) {}
                        });
                    } catch (Exception ignored) {}
                }
            }
        });
    }

    // ------------------------------------------------------------------

    private static HttpResponse<String> get(int port, String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path))
                .timeout(Duration.ofSeconds(5)).GET().build();
        return HTTP.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private static Json.Obj getJson(int port, String path) throws Exception {
        return (Json.Obj) Json.parse(get(port, path).body());
    }

    private static HttpResponse<String> postRaw(int port, String path, String body) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path))
                .timeout(Duration.ofSeconds(5))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build();
        return HTTP.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private static Json.Obj post(int port, String path, String body) throws Exception {
        HttpResponse<String> resp = postRaw(port, path, body);
        if (resp.statusCode() != 200) {
            throw new AssertionError("POST " + path + " -> " + resp.statusCode() + ": " + resp.body());
        }
        return (Json.Obj) Json.parse(resp.body());
    }
}
