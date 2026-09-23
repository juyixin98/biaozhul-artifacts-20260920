package test;

import txsnapshot.ApiServer;
import txsnapshot.CheckpointStore;
import txsnapshot.Fault;
import txsnapshot.Json;
import txsnapshot.StreamEngine;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ExecutionException;

import static test.Assert.check;
import static test.Assert.eq;

/**
 * 事务性流快照验收测试。
 *
 * 覆盖：
 *  - 正常路径
 *  - 四个故障点（处理中 / prepare 中 / 落盘后提交前 / commit 中），
 *    分别用进程内 EXCEPTION 与真实子进程 HALT(kill) 两种方式注入
 *  - 故障后重放：可见输出无遗漏、无重复，偏移恰好 0..n-1，状态一致
 *  - 崩溃现场磁盘文件形态（半行 commit、残段、悬空事务、快照 pendingCommit）
 *  - 重复恢复的幂等性
 *  - HTTP 端到端（含故障注入后换进程语义的“重启”）
 */
public final class Tests {

    private static final Assert A = new Assert();
    private static Path tmpRoot;

    // offset i 的输入值
    private static long value(long offset) { return 10L + offset; }
    private static long expectedState(int n) {
        long s = 0;
        for (int i = 0; i < n; i++) s += value(i);
        return s;
    }

    public static void main(String[] args) throws Exception {
        tmpRoot = Files.createTempDirectory("txsnapshot-tests-");
        System.out.println("tmp root: " + tmpRoot);

        happyPath();
        for (Fault.Point p : Fault.Point.values()) {
            inProcessCrashTest(p);
        }
        crashSiteInspection();
        doubleReopenIdempotent();
        for (Fault.Point p : Fault.Point.values()) {
            subprocessHaltTest(p);
        }
        httpEndToEnd();
        verifierCliTests();

        int rc = A.finish();
        System.exit(rc);
    }

    // ---------------- 正常路径 ----------------

    private static void happyPath() throws Exception {
        A.run("happy path: 5 inputs -> state, 5 visible outputs in order", () -> {
            Path d = freshDir();
            try (StreamEngine e = StreamEngine.open(d, new Fault())) {
                ingest(e, 5);
                eq(e.status().get("state"), expectedState(5), "state");
                List<Map<String, Object>> out = e.outputs();
                eq(out.size(), 5, "output count");
                assertExactlyOnce(out, 5);
                eq(((Number) e.status().get("offset")).longValue(), 4L, "durable offset");
            }
        });
    }

    // ---------------- 进程内 EXCEPTION 故障（四个注入点） ----------------

    private static void inProcessCrashTest(Fault.Point point) {
        A.run("EXCEPTION fault at " + point + " then reopen + continue: exactly-once", () -> {
            Path d = freshDir();
            try (StreamEngine e = StreamEngine.open(d, new Fault())) {
                ingest(e, 2); // offset 0,1 正常完成
                e.fault().arm(point, Fault.Mode.EXCEPTION, 2);
                boolean threw = false;
                try {
                    e.ingest(value(2));
                } catch (ExecutionException ex) {
                    threw = String.valueOf(ex.getCause()).contains("injected fault at " + point);
                }
                check(threw, "expected injected fault ExecutionException at " + point);
            }

            // “重启”：同一目录重新打开，引擎自动恢复 + 重放
            try (StreamEngine e2 = StreamEngine.open(d, new Fault())) {
                List<Map<String, Object>> outAfterReopen = e2.outputs();
                eq(outAfterReopen.size(), 3,
                        point + ": offset 2 must be visible after replay (no gap)");
                assertExactlyOnce(outAfterReopen, 3);
                eq(((Number) e2.status().get("state")).longValue(), expectedState(3),
                        point + ": state after replay");

                // 继续处理到 5 条
                long o3 = e2.ingest(value(3));
                long o4 = e2.ingest(value(4));
                eq(o3, 3L, "offset after recovery 1");
                eq(o4, 4L, "offset after recovery 2");
                List<Map<String, Object>> out = e2.outputs();
                eq(out.size(), 5, point + ": total visible outputs");
                assertExactlyOnce(out, 5);
                eq(((Number) e2.status().get("state")).longValue(), expectedState(5),
                        point + ": final state");
                eq(((Number) e2.status().get("offset")).longValue(), 4L,
                        point + ": final durable offset");
            }
            assertCommitLogNoDupes(d);
        });
    }

    // ---------------- 崩溃现场的磁盘形态 ----------------

    private static void crashSiteInspection() {
        A.run("crash-site on-disk shapes (torn marker / half segment / pending snapshot)", () -> {
            // 1) commit 半行
            Path d1 = freshDir();
            try (StreamEngine e = StreamEngine.open(d1, new Fault())) {
                ingest(e, 2);
                e.fault().arm(Fault.Point.DURING_COMMIT, Fault.Mode.EXCEPTION, 2);
                expectFault(e);
            }
            String marker = Files.readString(d1.resolve("sink/commit.log"), StandardCharsets.UTF_8);
            check(!marker.endsWith("\n"), "torn commit marker must have no trailing newline");
            check(marker.endsWith("txn"), "torn marker tail, got: " + marker.strip());
            check(Files.exists(d1.resolve("sink/segments/txn-2.rec")), "prepared segment exists");
            CheckpointStore.Snapshot s1 = new CheckpointStore(d1.resolve("checkpoints")).loadLatest();
            eq(s1.offset, 2L, "snapshot offset");
            eq(s1.pendingCommit, "txn-2", "snapshot pendingCommit");

            // 2) prepare 残段：存在但不是完整 JSON 行
            Path d2 = freshDir();
            try (StreamEngine e = StreamEngine.open(d2, new Fault())) {
                ingest(e, 2);
                e.fault().arm(Fault.Point.DURING_PREPARE, Fault.Mode.EXCEPTION, 2);
                expectFault(e);
            }
            Path seg2 = d2.resolve("sink/segments/txn-2.rec");
            check(Files.exists(seg2), "half-written segment exists");
            boolean parseFailed = false;
            try {
                Json.object(Files.readString(seg2, StandardCharsets.UTF_8).trim());
            } catch (RuntimeException bad) {
                parseFailed = true;
            }
            check(parseFailed, "half-written segment must not parse as a record");
            CheckpointStore.Snapshot s2 = new CheckpointStore(d2.resolve("checkpoints")).loadLatest();
            eq(s2.offset, 1L, "snapshot stays at previous offset after prepare crash");

            // 3) 处理中崩溃：没有段、没有新快照、输入日志已包含该条（fsync 在前）
            Path d3 = freshDir();
            try (StreamEngine e = StreamEngine.open(d3, new Fault())) {
                ingest(e, 2);
                e.fault().arm(Fault.Point.DURING_PROCESSING, Fault.Mode.EXCEPTION, 2);
                expectFault(e);
            }
            check(!Files.exists(d3.resolve("sink/segments/txn-2.rec")),
                    "no segment when crash during processing");
            CheckpointStore.Snapshot s3 = new CheckpointStore(d3.resolve("checkpoints")).loadLatest();
            eq(s3.offset, 1L, "snapshot stays at previous offset after processing crash");
            String inputLog = Files.readString(d3.resolve("input.log"), StandardCharsets.UTF_8);
            check(inputLog.contains("\"offset\":2"), "crashed input is durable in input.log");

            // 4) 落盘后提交前：快照 pending，段完整，commit.log 无该事务
            Path d4 = freshDir();
            try (StreamEngine e = StreamEngine.open(d4, new Fault())) {
                ingest(e, 2);
                e.fault().arm(Fault.Point.AFTER_STATE_PERSISTED, Fault.Mode.EXCEPTION, 2);
                expectFault(e);
            }
            CheckpointStore.Snapshot s4 = new CheckpointStore(d4.resolve("checkpoints")).loadLatest();
            eq(s4.offset, 2L, "snapshot offset advanced");
            eq(s4.pendingCommit, "txn-2", "snapshot records pending commit");
            Map<String, Object> rec = Json.object(
                    Files.readString(d4.resolve("sink/segments/txn-2.rec"), StandardCharsets.UTF_8).trim());
            eq(Json.lng(rec, "offset"), 2L, "prepared record readable");
            String markers = Files.readString(d4.resolve("sink/commit.log"), StandardCharsets.UTF_8);
            check(!markers.contains("txn-2"), "commit marker absent before commit");
        });
    }

    // ---------------- 二次恢复幂等 ----------------

    private static void doubleReopenIdempotent() {
        A.run("reopen twice after crash: idempotent, still exactly-once", () -> {
            Path d = freshDir();
            try (StreamEngine e = StreamEngine.open(d, new Fault())) {
                ingest(e, 3);
                e.fault().arm(Fault.Point.AFTER_STATE_PERSISTED, Fault.Mode.EXCEPTION, 3);
                expectFault(e);
            }
            try (StreamEngine ignored = StreamEngine.open(d, new Fault())) {
                eq(ignored.outputs().size(), 4, "first reopen commits pending txn");
            }
            try (StreamEngine e2 = StreamEngine.open(d, new Fault())) {
                eq(e2.outputs().size(), 4, "second reopen adds nothing");
                e2.ingest(value(4));
                eq(e2.outputs().size(), 5, "continues after repeated recovery");
                assertExactlyOnce(e2.outputs(), 5);
            }
            assertCommitLogNoDupes(d);
        });
    }

    // ---------------- 真实子进程 HALT（kill -9 语义） ----------------

    private static void subprocessHaltTest(Fault.Point point) {
        A.run("subprocess Runtime.halt at " + point + " then recover: exactly-once", () -> {
            Path d = freshDir();
            ProcessBuilder pb = new ProcessBuilder(
                    javaBinary(), "-cp", System.getProperty("java.class.path"),
                    "test.SubProcessDriver", d.toString(), "2", point.name(), "2");
            pb.redirectErrorStream(false);
            Process p = pb.start();
            String err = new String(p.getErrorStream().readAllBytes(), StandardCharsets.UTF_8);
            boolean exited = p.waitFor(30, java.util.concurrent.TimeUnit.SECONDS);
            check(exited, "subprocess did not exit (fault did not halt it?)");
            eq(p.exitValue(), 0, "subprocess exit code; stderr:\n" + err);
            check(err.contains("[fault] injected at " + point),
                    "fault log missing; stderr:\n" + err);

            // 父进程视角 = 进程已被杀掉，直接在同目录打开恢复
            try (StreamEngine e = StreamEngine.open(d, new Fault())) {
                eq(e.outputs().size(), 3, point + ": no gap after kill");
                assertExactlyOnce(e.outputs(), 3);
                e.ingest(value(3));
                e.ingest(value(4));
                List<Map<String, Object>> out = e.outputs();
                eq(out.size(), 5, point + ": total outputs after kill+recovery");
                assertExactlyOnce(out, 5);
                eq(((Number) e.status().get("state")).longValue(), expectedState(5),
                        point + ": state after kill+recovery");
            }
            assertCommitLogNoDupes(d);
        });
    }

    // ---------------- HTTP 端到端 ----------------

    private static void httpEndToEnd() throws Exception {
        A.run("HTTP end-to-end: ingest -> fault(EXCEPTION) -> restart -> outputs", () -> {
            Path d = freshDir();
            ApiServer api1 = ApiServer.start(d, 0, true);
            int port1 = api1.getPort();
            HttpClient http = HttpClient.newHttpClient();

            for (int i = 0; i < 2; i++) post(http, port1, "/ingest", "{\"value\":" + value(i) + "}");

            // 武装故障并触发
            post(http, port1, "/debug/fault",
                    "{\"point\":\"AFTER_STATE_PERSISTED\",\"mode\":\"EXCEPTION\",\"offset\":2}");
            HttpResponse<String> crashed = post(http, port1, "/ingest",
                    "{\"value\":" + value(2) + "}");
            check(crashed.statusCode() == 500 || crashed.statusCode() == 503,
                    "crashed ingest should fail 5xx, got " + crashed.statusCode());
            HttpResponse<String> down = post(http, port1, "/ingest", "{\"value\":99}");
            eq(down.statusCode(), 503, "engine refuses while crashed");

            // “重启服务”：同目录重新起一个实例
            api1.stop();
            ApiServer api2 = ApiServer.start(d, 0, true);
            int port2 = api2.getPort();
            try {
                for (int i = 3; i < 5; i++) {
                    HttpResponse<String> r = post(http, port2, "/ingest", "{\"value\":" + value(i) + "}");
                    eq(r.statusCode(), 200, "ingest after restart " + i);
                }
                HttpResponse<String> out = get(http, port2, "/outputs");
                Map<String, Object> body = Json.object(out.body());
                @SuppressWarnings("unchecked")
                List<Map<String, Object>> outputs = (List<Map<String, Object>>) body.get("outputs");
                eq(outputs.size(), 5, "visible outputs via HTTP after restart");
                assertExactlyOnce(outputs, 5);

                HttpResponse<String> st = get(http, port2, "/state");
                Map<String, Object> state = Json.object(st.body());
                eq(Json.lng(state, "state"), expectedState(5), "state via HTTP");
                eq(Json.lng(state, "offset"), 4L, "offset via HTTP");
            } finally {
                api2.stop();
            }
            assertCommitLogNoDupes(d);
        });
    }

    // ---------------- 校验器 CLI ----------------

    private static void verifierCliTests() {
        A.run("verifier CLI: FAIL on crashed dir, PASS after recovery", () -> {
            Path d = freshDir();
            try (StreamEngine e = StreamEngine.open(d, new Fault())) {
                ingest(e, 2);
                e.fault().arm(Fault.Point.DURING_COMMIT, Fault.Mode.EXCEPTION, 2);
                expectFault(e);
            }
            Result bad = runVerifier(d);
            eq(bad.code, 1, "verifier must flag post-crash pre-recovery state");
            check(bad.out.contains("torn tail"), "verifier mentions torn tail");

            try (StreamEngine e = StreamEngine.open(d, new Fault())) {
                e.ingest(value(3));
                e.ingest(value(4));
            }
            Result good = runVerifier(d);
            eq(good.code, 0, "verifier passes after recovery:\n" + good.out);
            check(good.out.contains("RESULT: PASS"), "pass marker");
            check(good.out.contains("visible (committed) outputs: 5"), "count line");
        });
    }

    // ---------------- 公共断言/工具 ----------------

    /** 可见输出恰好是 offset 0..n-1 各一次，且 result = value^2。 */
    private static void assertExactlyOnce(List<Map<String, Object>> out, int n) {
        eq(out.size(), n, "output count");
        Set<Long> offsets = new HashSet<>();
        for (Map<String, Object> rec : out) {
            long off = Json.lng(rec, "offset");
            long val = Json.lng(rec, "value");
            long res = Json.lng(rec, "result");
            eq(val, value(off), "value/offset relation at " + off);
            eq(res, val * val, "operator result at " + off);
            check(offsets.add(off), "duplicate visible offset " + off);
        }
        for (long o = 0; o < n; o++) {
            check(offsets.contains(o), "missing visible offset " + o);
        }
    }

    private static void assertCommitLogNoDupes(Path d) throws Exception {
        Path log = d.resolve("sink/commit.log");
        if (!Files.exists(log)) return;
        List<String> lines = new ArrayList<>();
        for (String l : Files.readString(log, StandardCharsets.UTF_8).split("\n", -1)) {
            if (!l.isEmpty()) lines.add(l);
        }
        eq(lines.size(), new HashSet<>(lines).size(), "commit.log contains duplicate markers");
    }

    private static void ingest(StreamEngine e, int n) throws Exception {
        for (int i = 0; i < n; i++) e.ingest(value(i));
    }

    private static void expectFault(StreamEngine e) {
        boolean threw = false;
        try {
            e.ingest(value(((Number) e.status().get("nextInputOffset")).longValue()));
        } catch (ExecutionException ex) {
            threw = String.valueOf(ex.getCause()).contains("injected fault");
        } catch (Exception other) {
            throw new AssertionError("unexpected exception " + other);
        }
        check(threw, "expected injected fault");
    }

    private static Path freshDir() throws Exception {
        Path d = Files.createTempDirectory(tmpRoot, "case-");
        return d;
    }

    private static String javaBinary() {
        return Path.of(System.getProperty("java.home"), "bin", "java").toString();
    }

    private static HttpResponse<String> post(HttpClient http, int port, String path, String json)
            throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json, StandardCharsets.UTF_8))
                .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private static HttpResponse<String> get(HttpClient http, int port, String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path))
                .GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private record Result(int code, String out) {}

    private static Result runVerifier(Path d) throws Exception {
        ProcessBuilder pb = new ProcessBuilder(javaBinary(),
                "-cp", System.getProperty("java.class.path"),
                "txsnapshot.Main", "--verify", "--data", d.toString());
        pb.redirectErrorStream(true);
        Process p = pb.start();
        String out = new String(p.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
        p.waitFor(10, java.util.concurrent.TimeUnit.SECONDS);
        return new Result(p.exitValue(), out);
    }
}
