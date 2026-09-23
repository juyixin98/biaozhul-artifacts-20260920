package com.example.cdc;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.time.Duration;
import java.util.List;
import java.util.Map;

/** HTTP 端到端测试：真实启动 JDK HttpServer（随机端口），用 java.net.http 调用。 */
final class HttpTest {

    private static final HttpClient HTTP = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(5))
            .build();

    private static Path tmpDir() throws Exception {
        Path d = java.nio.file.Files.createTempDirectory("cdc-http-");
        d.toFile().deleteOnExit();
        return d;
    }

    private static final class Node {
        final Wal wal;
        final Engine engine;
        final ApiServer api;

        Node(Wal wal, Engine engine, ApiServer api) {
            this.wal = wal;
            this.engine = engine;
            this.api = api;
        }

        void stop() {
            api.stop();
            wal.close();
        }
    }

    private static Node start(Path dir, int port) {
        Wal wal = Wal.open(dir.resolve("cdc.wal"));
        Engine engine = new Engine(wal);
        engine.recover();
        ApiServer api = new ApiServer(port, engine, wal);
        api.start();
        return new Node(wal, engine, api);
    }

    private static String url(int port, String path) {
        return "http://127.0.0.1:" + port + path;
    }

    private static HttpResponse<String> post(int port, String path, String json) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url(port, path)))
                .timeout(Duration.ofSeconds(5))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json, StandardCharsets.UTF_8))
                .build();
        return HTTP.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private static HttpResponse<String> get(int port, String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url(port, path)))
                .timeout(Duration.ofSeconds(5))
                .GET().build();
        return HTTP.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> obj(HttpResponse<String> r) {
        return (Map<String, Object>) Json.parse(r.body());
    }

    public static void register() {

        Test.it("HTTP 全流程：投递/不可见/提交/查询/对账", () -> {
            Path dir = tmpDir();
            Node n = start(dir, 0);
            int p = n.api.port();
            try {
                String payload = Json.write(List.of(
                        Ev.insert(1, "T1", "users", List.of(1), Ev.v("id", 1L, "name", "alice")),
                        Ev.pkChange(2, "T1", "users", List.of(1), List.of(7),
                                Ev.v("id", 7L, "name", "alice2"))));
                HttpResponse<String> r1 = post(p, "/events", payload);
                Test.eq(r1.statusCode(), 200, "投递 2 条 DATA 应 200: " + r1.body());

                HttpResponse<String> inv = get(p, "/tables/users");
                Test.eq(inv.body().replaceAll("\\s", ""), "[]", "提交前表为空");

                HttpResponse<String> rc = post(p, "/events", Json.write(List.of(Ev.commit(3, "T1"))));
                Test.eq(rc.statusCode(), 200, "COMMIT 应 200");

                HttpResponse<String> rows = get(p, "/tables/users");
                String rb = rows.body().replaceAll("\\s", "");
                Test.check(rb.contains("\"id\":7") && rb.contains("alice2"),
                        "改主键后应只有新主键行: " + rows.body());
                Test.check(!rb.contains("\"id\":1"), "旧主键行必须消失: " + rows.body());

                HttpResponse<String> rec = get(p, "/reconcile");
                Test.check(obj(rec).get("match").equals(Boolean.TRUE),
                        "对账必须一致: " + rec.body());

                HttpResponse<String> chg = get(p, "/changes?table=users");
                Test.check(chg.body().contains("commitPosition"), "台账含源位置");

                HttpResponse<String> st = get(p, "/status");
                Test.check(st.body().contains("RUNNING"), "状态 RUNNING");
            } finally {
                n.stop();
            }
        });

        Test.it("HTTP 回滚后查询为空，且重复投递幂等", () -> {
            Path dir = tmpDir();
            Node n = start(dir, 0);
            int p = n.api.port();
            try {
                post(p, "/events", Json.write(List.of(
                        Ev.insert(1, "T1", "t", List.of(1), Ev.v("v", 1L)))));
                post(p, "/events", Json.write(List.of(Ev.rollback(2, "T1"))));
                Test.eq(get(p, "/tables/t").body().replaceAll("\\s", ""), "[]", "回滚后为空");

                HttpResponse<String> dup = post(p, "/events", Json.write(List.of(
                        Ev.insert(1, "T1", "t", List.of(1), Ev.v("v", 1L)),
                        Ev.rollback(2, "T1"))));
                Map<String, Object> m = obj(dup);
                Test.eq(m.get("duplicates"), 2L, "两条均判重");
                Test.eq(get(p, "/tables/t").body().replaceAll("\\s", ""), "[]", "重发不改变状态");
            } finally {
                n.stop();
            }
        });

        Test.it("HTTP 缺口返回 PAUSED，补齐后 RUNNING", () -> {
            Path dir = tmpDir();
            Node n = start(dir, 0);
            int p = n.api.port();
            try {
                post(p, "/events", Json.write(List.of(
                        Ev.insert(1, "T1", "t", List.of(1), Ev.v("v", 1L)),
                        Ev.commit(2, "T1"))));
                HttpResponse<String> gap = post(p, "/events",
                        Json.write(List.of(Ev.insert(5, "T9", "t", List.of(9), Ev.v("v", 9L)))));
                Test.check(gap.body().contains("PAUSED"), "应报告 PAUSED: " + gap.body());
                List<?> beforeFill = (List<?>) Json.parse(get(p, "/tables/t").body());
                Test.eq(beforeFill.size(), 1, "只有已提交的 1 行（T9 滞留不可见）");

                post(p, "/events", Json.write(List.of(
                        Ev.insert(3, "T2", "t", List.of(2), Ev.v("v", 2L)))));
                Test.check(get(p, "/status").body().contains("PAUSED"),
                        "还差位置 4，继续暂停，位置 5 不得提前生效");
                // 位置 4 提交 T2：3、4 连续排空；5 是 T9 未提交 DATA，仍不可见
                HttpResponse<String> done = post(p, "/events", Json.write(List.of(
                        Ev.commit(4, "T2"))));
                Test.check(done.body().contains("RUNNING"), "补齐排空后 RUNNING: " + done.body());
                List<?> rowsList = (List<?>) Json.parse(get(p, "/tables/t").body());
                Test.eq(rowsList.size(), 2,
                        "T1 与 T2 各一行（T9 只有滞留的位置5 DATA，未提交不可见）");
            } finally {
                n.stop();
            }
        });

        Test.it("HTTP 400：非法 JSON / 非法字段 / 未知事务，均不污染状态", () -> {
            Path dir = tmpDir();
            Node n = start(dir, 0);
            int p = n.api.port();
            try {
                Test.eq(post(p, "/events", "not-json").statusCode(), 400, "非法 JSON 应 400");
                Test.eq(post(p, "/events", Json.write(List.of(Ev.insert(0, "T1", "t",
                        List.of(1), Ev.v())))).statusCode(), 400, "position=0 应 400");
                Test.eq(post(p, "/events", Json.write(List.of(Ev.commit(1, "GHOST")))
                ).statusCode(), 400, "未知事务 COMMIT 应 400");
                Test.eq(get(p, "/status").body().contains("\"watermark\" : 0")
                                || get(p, "/status").body().contains("\"watermark\": 0"),
                        true, "拒绝后水位仍为 0");
                // 合法事件照常工作
                Test.eq(post(p, "/events", Json.write(List.of(
                        Ev.insert(1, "T1", "t", List.of(1), Ev.v("v", 1L)),
                        Ev.commit(2, "T1")))).statusCode(), 200, "合法事件应 200");
            } finally {
                n.stop();
            }
        });

        Test.it("HTTP 跨重启半事务：关服务再开，事务仍 OPEN，补 COMMIT 后可见", () -> {
            Path dir = tmpDir();
            Node n1 = start(dir, 0);
            int port = n1.api.port();
            post(port, "/events", Json.write(List.of(
                    Ev.insert(1, "T1", "users", List.of(1), Ev.v("name", "a")),
                    Ev.insert(2, "T1", "users", List.of(2), Ev.v("name", "b")))));
            Test.eq(get(port, "/tables/users").body().replaceAll("\\s", ""), "[]", "提交前不可见");
            n1.stop();

            Node n2 = start(dir, port); // 同端口“重启”
            try {
                HttpResponse<String> txn = get(port, "/txns/T1");
                Test.check(txn.body().contains("\"open\" : true")
                                || txn.body().contains("\"open\": true"),
                        "重启后 T1 仍 OPEN: " + txn.body());
                Test.check(txn.body().contains("\"dataCount\" : 2")
                                || txn.body().contains("\"dataCount\": 2"),
                        "两条 DATA 均恢复: " + txn.body());
                Test.eq(get(port, "/tables/users").body().replaceAll("\\s", ""), "[]",
                        "重启后仍不可见");
                post(port, "/events", Json.write(List.of(Ev.commit(3, "T1"))));
                List<?> rows = (List<?>) Json.parse(get(port, "/tables/users").body());
                Test.eq(rows.size(), 2, "重启后提交，两行应可见");
                HttpResponse<String> rec = get(port, "/reconcile");
                Test.check(obj(rec).get("match").equals(Boolean.TRUE),
                        "重启提交后对账一致: " + rec.body());
            } finally {
                n2.stop();
            }
        });

        Test.it("POST /reset 清空状态与 WAL", () -> {
            Path dir = tmpDir();
            Node n = start(dir, 0);
            int p = n.api.port();
            try {
                post(p, "/events", Json.write(List.of(
                        Ev.insert(1, "T1", "t", List.of(1), Ev.v("v", 1L)),
                        Ev.commit(2, "T1"))));
                Test.check(!get(p, "/tables/t").body().replaceAll("\\s", "").equals("[]"),
                        "reset 前应有数据");
                Test.eq(post(p, "/reset", "").statusCode(), 200, "reset 200");
                Test.eq(get(p, "/tables/t").body().replaceAll("\\s", ""), "[]", "reset 后为空");
                Test.check(get(p, "/status").body().contains("0"), "水位归零");
            } finally {
                n.stop();
            }
        });
    }
}
