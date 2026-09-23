package cep;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.List;
import java.util.Map;

/**
 * 验收 3（进程级）：真实地把服务作为独立 JVM 子进程启动，写入事件，
 * 调用 /test/crash 触发 Runtime.halt(1)（模拟 kill -9 / 断电，不执行任何
 * 优雅清理），然后用同一数据目录重新启动子进程，校验：
 *   - 已确认（崩溃前 HTTP 200）的匹配一个不丢
 *   - 崩溃时未完成的部分匹配（等待中的 A / A->B）也完整保留
 *   - 时间水位与输入序号连续
 *   - 重启后可继续写入并接续匹配
 */
public class RecoveryProcessTest {

    private Path dir;
    private Process process;

    private static String javaBin() {
        String javaHome = System.getProperty("java.home");
        String exec = System.getProperty("os.name").toLowerCase().contains("win")
                ? "java.exe" : "java";
        return javaHome != null ? Path.of(javaHome, "bin", exec).toString() : exec;
    }

    /** 启动服务子进程，轮询 /health 直到就绪，返回端口。 */
    private int startServer() throws Exception {
        if (dir == null) {
            dir = Files.createTempDirectory("cep-crash-test-");
        }
        String classpath = System.getProperty("java.class.path");
        ProcessBuilder pb = new ProcessBuilder(
                javaBin(), "-cp", classpath,
                "cep.Main",
                "--port=0",
                "--data-dir=" + dir.toAbsolutePath(),
                "--test-endpoints");
        pb.redirectErrorStream(true);
        process = pb.start();

        // 抓取输出，同时从日志中解析实际端口（port=0 时端口由系统分配）。
        StringBuilder output = new StringBuilder();
        java.io.InputStream in = process.getInputStream();
        long deadline = System.currentTimeMillis() + 20_000;
        int port = -1;
        byte[] buf = new byte[4096];
        while (System.currentTimeMillis() < deadline) {
            while (in.available() > 0) {
                int n = in.read(buf);
                String chunk = new String(buf, 0, n, StandardCharsets.UTF_8);
                output.append(chunk);
                int idx = output.indexOf("监听 http://127.0.0.1:");
                if (idx >= 0 && port < 0) {
                    int start = idx + "监听 http://127.0.0.1:".length();
                    int end = start;
                    while (end < output.length() && Character.isDigit(output.charAt(end))) {
                        end++;
                    }
                    port = Integer.parseInt(output.substring(start, end));
                }
            }
            if (port > 0 && process.isAlive()) {
                if (waitForHealth(port, 3000)) {
                    return port;
                }
            }
            if (!process.isAlive()) {
                throw new IllegalStateException("服务子进程提前退出，输出:\n" + output);
            }
            Thread.sleep(50);
        }
        throw new IllegalStateException("服务子进程 20s 内未就绪，输出:\n" + output);
    }

    private static boolean waitForHealth(int port, long timeoutMs) throws Exception {
        long deadline = System.currentTimeMillis() + timeoutMs;
        HttpClient c = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(1)).build();
        while (System.currentTimeMillis() < deadline) {
            try {
                HttpResponse<String> r = c.send(
                        HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + "/health"))
                                .timeout(Duration.ofSeconds(1)).GET().build(),
                        HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
                if (r.statusCode() == 200) {
                    return true;
                }
            } catch (Exception ignore) {
                // 连接被拒时继续轮询
            }
            Thread.sleep(100);
        }
        return false;
    }

    private static HttpResponse<String> req(int port, String method, String path, String body)
            throws Exception {
        HttpClient c = HttpClient.newHttpClient();
        HttpRequest.Builder b = HttpRequest.newBuilder(
                        URI.create("http://127.0.0.1:" + port + path))
                .timeout(Duration.ofSeconds(5));
        if (body == null) {
            b.GET();
        } else {
            b.header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8));
        }
        return c.send(b.build(), HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    @SuppressWarnings("unchecked")
    private static long matchCount(int port) throws Exception {
        Map<String, Object> resp = (Map<String, Object>) Json.parse(
                req(port, "GET", "/matches", null).body());
        return ((Number) resp.get("count")).longValue();
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> stateEntities(int port) throws Exception {
        Map<String, Object> resp = (Map<String, Object>) Json.parse(
                req(port, "GET", "/state", null).body());
        return (List<Map<String, Object>>) resp.get("entities");
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> entityState(int port, String entity) throws Exception {
        Map<String, Object> resp = (Map<String, Object>) Json.parse(
                req(port, "GET", "/state?entityId=" + entity, null).body());
        List<Map<String, Object>> entities = (List<Map<String, Object>>) resp.get("entities");
        return entities.isEmpty() ? Map.of() : entities.get(0);
    }

    /**
     * 完整崩溃-恢复剧本：
     * 1) 写入 A,A,B,C（实体 done）-> 2 个已完成匹配
     * 2) 写入 A,B（实体 partial）-> 留 1 个等待中的 A->B
     * 3) 硬崩溃
     * 4) 重启：匹配数仍为 2；partial 的 waitingAB 仍在；序号水位连续
     * 5) 对 partial 补 C -> 恢复后的部分匹配成功命中
     * 6) 写入新 A,B,C -> 新事件序号接续，匹配数正确
     */
    public void testCrashAndRestoreConsistency() throws Exception {
        int port1 = startServer();
        try {
            // 1) 已完成匹配
            HttpResponse<String> r1 = req(port1, "POST", "/events",
                    "[{\"type\":\"A\",\"entityId\":\"done\",\"timestamp\":1000},"
                            + "{\"type\":\"A\",\"entityId\":\"done\",\"timestamp\":1000},"
                            + "{\"type\":\"B\",\"entityId\":\"done\",\"timestamp\":2000},"
                            + "{\"type\":\"C\",\"entityId\":\"done\",\"timestamp\":3000}]");
            TestRunner.checkEq(r1.statusCode(), 200, "崩溃前写入 done 批次应成功");
            TestRunner.checkEq(matchCount(port1), 2, "崩溃前应有 2 个匹配");

            // 2) 未完成的部分匹配
            HttpResponse<String> r2 = req(port1, "POST", "/events",
                    "[{\"type\":\"A\",\"entityId\":\"partial\",\"timestamp\":7000},"
                            + "{\"type\":\"B\",\"entityId\":\"partial\",\"timestamp\":8000}]");
            TestRunner.checkEq(r2.statusCode(), 200, "崩溃前写入 partial 批次应成功");

            // 留一个窗口内很快就会过期的 A，验证恢复后过期清除行为也一致
            HttpResponse<String> r3 = req(port1, "POST", "/events",
                    "[{\"type\":\"A\",\"entityId\":\"lonely\",\"timestamp\":0}]");
            TestRunner.checkEq(r3.statusCode(), 200, "崩溃前写入 lonely 批次应成功");

            // 3) 硬崩溃（halt，非 0 退出，无 shutdown hook）
            HttpResponse<String> crash = req(port1, "POST", "/test/crash", "{}");
            TestRunner.checkEq(crash.statusCode(), 200, "崩溃端点应先回 200 再退出");
            if (!process.waitFor(10, java.util.concurrent.TimeUnit.SECONDS)) {
                TestRunner.fail("子进程应在崩溃请求后 10s 内退出");
            }
            TestRunner.check(process.exitValue() != 0, "硬崩溃退出码应非 0，实际 "
                    + process.exitValue());
        } finally {
            if (process != null && process.isAlive()) {
                process.destroyForcibly();
            }
        }

        // 4) 用同一数据目录重启
        int port2 = startServer();
        try {
            // 已确认的匹配不丢、明细不变
            TestRunner.checkEq(matchCount(port2), 2,
                    "恢复后已完成匹配必须仍是 2 个（不丢）");
            HttpResponse<String> doneMatches = req(port2, "GET", "/matches?entityId=done", null);
            TestRunner.check(doneMatches.body().contains("\"seq\":0")
                            && doneMatches.body().contains("\"seq\":1"),
                    "恢复后匹配明细中的输入序号必须保留: " + doneMatches.body());

            // 部分匹配状态完整保留
            @SuppressWarnings("unchecked")
            Map<String, Object> partialState = entityState(port2, "partial");
            List<?> waitingAB = (List<?>) partialState.get("waitingAB");
            TestRunner.checkEq(waitingAB.size(), 1,
                    "恢复后 partial 实体必须仍有 1 个等待中的 A->B");
            @SuppressWarnings("unchecked")
            Number wm = (Number) partialState.get("watermark");
            TestRunner.checkEq(wm.longValue(), 8000L, "恢复后水位应为 8000");

            // lonely 的 A 在时间 0，尚未有更晚事件触发清理时仍保留是合法的；
            // 推进一个 11 秒后的无关事件后它应被清除
            req(port2, "POST", "/events",
                    "[{\"type\":\"X\",\"entityId\":\"lonely\",\"timestamp\":11000}]");
            @SuppressWarnings("unchecked")
            Map<String, Object> lonelyAfter = entityState(port2, "lonely");
            TestRunner.check(((List<?>) lonelyAfter.get("waitingA")).isEmpty(),
                    "恢复后窗口过期语义与崩溃前一致：lonely 的旧 A 应被清除");

            // 5) 补 C，崩溃前留下的 A->B 在窗口内，必须命中
            HttpResponse<String> cont = req(port2, "POST", "/events",
                    "[{\"type\":\"C\",\"entityId\":\"partial\",\"timestamp\":9000}]");
            TestRunner.checkEq(cont.statusCode(), 200, "恢复后补 C 应成功");
            TestRunner.check(cont.body().contains("\"created\":1"),
                    "恢复后的部分匹配必须能继续完成: " + cont.body());
            TestRunner.checkEq(matchCount(port2), 3, "恢复并续配后总匹配应为 3");

            // 6) 序号连续：崩溃前已用 0..8（done 4 + partial 2 + lonely 1 + X 1 + 补 C 1 = 9），
            //    下一批新事件序号从 9 起
            HttpResponse<String> fresh = req(port2, "POST", "/events",
                    "[{\"type\":\"A\",\"entityId\":\"fresh\",\"timestamp\":20000},"
                            + "{\"type\":\"B\",\"entityId\":\"fresh\",\"timestamp\":20100},"
                            + "{\"type\":\"C\",\"entityId\":\"fresh\",\"timestamp\":20200}]");
            TestRunner.checkEq(fresh.statusCode(), 200, "恢复后写入全新批次应成功");
            TestRunner.check(fresh.body().contains("\"seq\":9")
                            && fresh.body().contains("\"seq\":10")
                            && fresh.body().contains("\"seq\":11"),
                    "恢复后新事件的输入序号必须接续 9,10,11: " + fresh.body());
            TestRunner.checkEq(matchCount(port2), 4, "最终总匹配应为 4");

            // 再次重启，幂等：多次恢复同一日志结果不变
        } finally {
            if (process != null && process.isAlive()) {
                process.destroyForcibly();
            }
        }

        int port3 = startServer();
        try {
            TestRunner.checkEq(matchCount(port3), 4, "第二次重启后匹配数仍应一致（恢复幂等）");
            // 推进一个窗口外的事件以触发清理：partial 的 A 锚点在 7000，
            // 到 18000 已超过 10 秒，残留的 A->B 应被清除（匹配仍在）。
            req(port3, "POST", "/events",
                    "[{\"type\":\"X\",\"entityId\":\"partial\",\"timestamp\":18000}]");
            @SuppressWarnings("unchecked")
            Map<String, Object> partial = entityState(port3, "partial");
            TestRunner.check(((List<?>) partial.get("waitingAB")).isEmpty(),
                    "窗口推进后已完成的 partial 不应再有等待中的 A->B");
            TestRunner.checkEq(matchCount(port3), 4, "清理部分匹配不影响已完成匹配");
        } finally {
            if (process != null && process.isAlive()) {
                process.destroyForcibly();
            }
        }
    }

    /**
     * 崩溃发生时“未获 200 确认”的半批数据：由于服务端总是先 fsync 再响应，
     * 本用例验证服务重启后序号与状态自洽（不会出现重复序号/幽灵匹配）。
     * 这里通过直接向日志末尾追加半行来模拟“写入途中崩溃”，等价于物理掉电。
     */
    public void testTornWriteOnCrashHasNoGhostState() throws Exception {
        int port1 = startServer();
        try {
            req(port1, "POST", "/events",
                    "[{\"type\":\"A\",\"entityId\":\"e\",\"timestamp\":0}]");
            if (!process.waitFor(2, java.util.concurrent.TimeUnit.SECONDS)) {
                // 服务仍活着才符合预期；这里不调崩溃端点，直接 destroyForcibly 模拟断电
            }
        } finally {
            if (process != null && process.isAlive()) {
                process.destroyForcibly();
                process.waitFor(5, java.util.concurrent.TimeUnit.SECONDS);
            }
        }

        // 手工制造半行未完成记录（模拟 write() 到一半掉电）
        Path logFile = dir.resolve("event.log");
        byte[] current = Files.readAllBytes(logFile);
        byte[] torn = "{\"events\":[{\"type\":\"B\",\"enti".getBytes(StandardCharsets.UTF_8);
        byte[] merged = new byte[current.length + torn.length];
        System.arraycopy(current, 0, merged, 0, current.length);
        System.arraycopy(torn, 0, merged, current.length, torn.length);
        Files.write(logFile, merged);

        int port2 = startServer();
        try {
            TestRunner.checkEq(matchCount(port2), 0, "半行记录不得产生幽灵匹配");
            @SuppressWarnings("unchecked")
            Map<String, Object> state = entityState(port2, "e");
            TestRunner.checkEq(((List<?>) state.get("waitingA")).size(), 1,
                    "已确认的 1 个 A 应保留，半行的 B 不得存在");

            // 序号接续：已确认的 A 占用 0，半批无效，下一个仍为 1
            HttpResponse<String> next = req(port2, "POST", "/events",
                    "[{\"type\":\"B\",\"entityId\":\"e\",\"timestamp\":100},"
                            + "{\"type\":\"C\",\"entityId\":\"e\",\"timestamp\":200}]");
            TestRunner.check(next.body().contains("\"seq\":1")
                            && next.body().contains("\"seq\":2"),
                    "半批作废后输入序号应接续为 1,2: " + next.body());
            TestRunner.checkEq(matchCount(port2), 1, "应正常产生 1 个匹配");
        } finally {
            if (process != null && process.isAlive()) {
                process.destroyForcibly();
            }
        }
    }
}
