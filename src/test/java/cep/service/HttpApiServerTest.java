package cep.service;

import cep.json.JsonParser;
import cep.json.JsonValue;
import cep.test.TestFramework;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

/** 启动真实 HTTP 服务做端到端验证：/evaluate、会话生命周期、重放、错误处理。 */
public final class HttpApiServerTest {

    private HttpApiServerTest() {}

    public static void register(TestFramework tf) {
        HttpApiServer server = new HttpApiServer(0);
        server.start();
        int port = server.getPort();
        // 用 127.0.0.1 而非 localhost：避免 JVM 把 localhost 解析到 IPv6 ::1
        String base = "http://127.0.0.1:" + port;
        HttpClient http = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(5))
                // 对所有地址返回 [NO_PROXY] = 直连（注意必须含元素，空列表会让客户端挂起）
                .proxy(new java.net.ProxySelector() {
                    @Override
                    public java.util.List<java.net.Proxy> select(java.net.URI uri) {
                        return java.util.List.of(java.net.Proxy.NO_PROXY);
                    }

                    @Override
                    public void connectFailed(java.net.URI uri,
                                              java.net.SocketAddress sa, java.io.IOException ioe) {
                        // 直连模式下无动作
                    }
                })
                .build();

        try {
            tf.addTest("HTTP: /healthz", unchecked(() -> {
                HttpResponse<String> resp = get(http, base + "/healthz");
                TestFramework.assertEquals(200, resp.statusCode(), "状态码");
                TestFramework.assertTrue(resp.body().contains("\"ok\""), "body: " + resp.body());
            }));

            tf.addTest("HTTP: /evaluate 多A+C打断+超时，返回事件ID", unchecked(() -> {
                String body = """
                    {
                      "pattern": {"a":"A","b":"B","c":"C","windowMs":1000,"policy":"ALL_PAIRS"},
                      "events": [
                        {"id":"A1","key":"A","timestamp":0},
                        {"id":"A2","key":"A","timestamp":100},
                        {"id":"C1","key":"C","timestamp":400},
                        {"id":"A3","key":"A","timestamp":600},
                        {"id":"B1","key":"B","timestamp":1000}
                      ]
                    }
                    """;
                HttpResponse<String> resp = post(http, base + "/evaluate", body);
                TestFramework.assertEquals(200, resp.statusCode(), resp.body());
                JsonValue.Obj out = JsonParser.parse(resp.body()).asObj();
                JsonValue.Arr matches = out.requireArr("matches");
                TestFramework.assertEquals(1, matches.size(), "1个匹配");
                TestFramework.assertEquals("A3", matches.get(0).asObj().requireString("aId"),
                        "匹配的A");
                TestFramework.assertEquals("B1", matches.get(0).asObj().requireString("bId"),
                        "匹配的B");
                TestFramework.assertEquals(2L, out.requireObj("stats").requireLong("cKilled"),
                        "cKilled");
            }));

            tf.addTest("HTTP: /evaluate useReference 交叉验证结果一致", unchecked(() -> {
                String body = """
                    {
                      "pattern": {"a":"A","b":"B","c":"C","windowMs":500,"policy":"EARLIEST_A"},
                      "useReference": true,
                      "events": [
                        {"id":"a1","key":"A","timestamp":0},
                        {"id":"a2","key":"A","timestamp":100},
                        {"id":"b1","key":"B","timestamp":300},
                        {"id":"c1","key":"C","timestamp":600},
                        {"id":"a3","key":"A","timestamp":700},
                        {"id":"b2","key":"B","timestamp":900}
                      ]
                    }
                    """;
                HttpResponse<String> resp = post(http, base + "/evaluate", body);
                TestFramework.assertEquals(200, resp.statusCode(), resp.body());
                JsonValue.Obj out = JsonParser.parse(resp.body()).asObj();
                TestFramework.assertTrue(out.requireBool("agreesWithReference"),
                        "流式与参考一致: " + resp.body());
            }));

            tf.addTest("HTTP: 会话 生命周期(创建/事件/水位/快照/flush/重放一致)", unchecked(() -> {
                // 创建
                String cfg = "{\"a\":\"A\",\"b\":\"B\",\"c\":\"C\",\"windowMs\":1000,"
                        + "\"policy\":\"ALL_PAIRS\"}";
                HttpResponse<String> created = post(http, base + "/api/sessions", cfg);
                TestFramework.assertEquals(201, created.statusCode(), created.body());
                String sid = JsonParser.parse(created.body()).asObj().requireString("sessionId");
                String url = base + "/api/sessions/" + sid;

                // 追加事件（批量）；水位语义下事件需 wm > ts 才处理，先推进水位
                HttpResponse<String> ev1 = post(http, url + "/events", """
                        {"events":[
                          {"id":"a1","key":"A","timestamp":0},
                          {"id":"a2","key":"A","timestamp":100},
                          {"id":"b1","key":"B","timestamp":400}
                        ]}""");
                TestFramework.assertEquals(200, ev1.statusCode(), ev1.body());
                post(http, url + "/watermark?watermark=401", "{}"); // 处理到 ts<401
                // 重新取快照：a1->b1、a2->b1 两个重叠匹配
                HttpResponse<String> afterWm1 = get(http, url);
                JsonValue.Obj snap1 = JsonParser.parse(afterWm1.body()).asObj();
                TestFramework.assertEquals(2, snap1.requireArr("matches").size(),
                        "两个重叠匹配");

                // 单事件追加
                HttpResponse<String> ev2 = post(http, url + "/events",
                        "{\"id\":\"c1\",\"key\":\"C\",\"timestamp\":500}");
                TestFramework.assertEquals(200, ev2.statusCode(), ev2.body());

                // 手动水位
                HttpResponse<String> wm = post(http, url + "/watermark?watermark=2000", "{}");
                TestFramework.assertEquals(200, wm.statusCode(), wm.body());
                TestFramework.assertEquals(2000L,
                        JsonParser.parse(wm.body()).asObj().requireLong("watermark"),
                        "水位被注入");

                // GET 快照
                HttpResponse<String> got = get(http, url);
                TestFramework.assertEquals(200, got.statusCode(), got.body());

                // flush
                HttpResponse<String> flushed = post(http, url + "/flush", "{}");
                TestFramework.assertEquals(200, flushed.statusCode(), flushed.body());
                String firstResult = normalizedResult(flushed.body());

                // flush 后追加 -> 409
                HttpResponse<String> after = post(http, url + "/events",
                        "{\"id\":\"x\",\"key\":\"X\",\"timestamp\":1}");
                TestFramework.assertEquals(409, after.statusCode(), "flush后拒绝事件");

                // 重放：结果与首次一致
                HttpResponse<String> replayed = post(http, url + "/replay", "{}");
                TestFramework.assertEquals(200, replayed.statusCode(), replayed.body());
                String secondResult = normalizedResult(replayed.body());
                TestFramework.assertEquals(firstResult, secondResult, "重放结果逐字节一致");
                TestFramework.assertTrue(
                        JsonParser.parse(replayed.body()).asObj().requireBool("replayed"),
                        "replayed 标记");
            }));

            tf.addTest("HTTP: 错误处理 404/400", unchecked(() -> {
                HttpResponse<String> missing = get(http, base + "/api/sessions/no-such-id");
                TestFramework.assertEquals(404, missing.statusCode(), "未知会话404");

                HttpResponse<String> badJson = post(http, base + "/evaluate", "{not json");
                TestFramework.assertEquals(400, badJson.statusCode(), "坏JSON 400");

                HttpResponse<String> badCfg = post(http, base + "/evaluate", """
                        {"pattern":{"a":"A","b":"A","c":"C","windowMs":1000},
                         "events":[]}""");
                TestFramework.assertEquals(400, badCfg.statusCode(), "非法配置 400");

                HttpResponse<String> wrongMethod = get(http, base + "/evaluate");
                TestFramework.assertEquals(400, wrongMethod.statusCode(), "GET /evaluate 400");
            }));
        } finally {
            // 注意：不能在这里停服务——register() 返回后测试才开始执行。
            // 服务由最后一个测试（见下方 teardown 注册项）关闭。
        }
        tf.addTest("HTTP: 测试结束关闭服务", () -> server.stop());
    }

    /** 把允许抛受检异常的测试体包装成 Runnable（异常向上抛由框架捕获计为失败）。 */
    private static Runnable unchecked(ThrowingRunnable body) {
        return () -> {
            try {
                body.run();
            } catch (RuntimeException | Error re) {
                throw re;
            } catch (Exception e) {
                throw new RuntimeException(e);
            }
        };
    }

    @FunctionalInterface
    private interface ThrowingRunnable {
        void run() throws Exception;
    }

    private static HttpResponse<String> post(HttpClient http, String url, String body)
            throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url))
                .timeout(Duration.ofSeconds(10))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private static HttpResponse<String> get(HttpClient http, String url) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url))
                .timeout(Duration.ofSeconds(10))
                .GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    /** 去掉会话级易变字段后比较结果主体，用于首跑与重放的逐字节对比。 */
    private static String normalizedResult(String body) {
        JsonValue.Obj o = JsonParser.parse(body).asObj();
        o.set("replayed", false); // 首次无此字段(false)，重放为true，对比时归一
        // watermarks 是"推进操作次数"：重放只重放事件、不含手动水位调用，次数会少，
        // 但不影响结果，对比时归一。
        if (o.has("stats")) {
            o.requireObj("stats").set("watermarks", 0L);
        }
        return cep.json.JsonWriter.write(o);
    }
}
