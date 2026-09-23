package com.example.txflow;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

/**
 * 崩溃恢复端到端测试（真正的进程级故障注入）。
 *
 * 流程（对四个故障点各跑一次，另加一个无故障对照组）：
 *
 *   1. 启动真实 HTTP 服务进程（java ... HttpApiServer，独立 JVM）；
 *   2. 追加 6 条输入；
 *   3. 先正常提交 2 批（offset 0,1，建立已提交基线）；
 *   4. 再处理一批（offset 2,3）并在指定故障点注入 Runtime.halt(17)
 *      —— 进程被硬终止，不执行任何清理；
 *   5. 用同一数据目录重启进程（启动自动恢复）；
 *   6. 把剩余记录全部处理完；
 *   7. 机械核对接收器【可见输出】：
 *        - inputOffset 集合恰为 {0,1,2,3,4,5}（无遗漏）；
 *        - 每个 offset 恰好出现一次（无重复）；
 *        - 顺序严格递增（重放没有乱序）；
 *        - 最终 word count a=9,b=6,c=3 与无故障结果一致；
 *        - committed.log 与快照对账一致。
 *
 * 这是任务的验收标准：“在处理、状态落盘、输出准备和提交之间注入故障，
 * 重放后核对接收器无遗漏、无重复可见输出”。
 */
public class CrashRecoveryTest {

    private static final String JAVA = resolveJava();
    private static final List<String> INPUTS = List.of(
            "{\"text\":\"a b\"}",      // off 0
            "{\"text\":\"a a c\"}",    // off 1
            "{\"text\":\"b c\"}",      // off 2  (崩溃批次)
            "{\"text\":\"a b c\"}",    // off 3  (崩溃批次)
            "{\"text\":\"a a a b\"}",  // off 4
            "{\"text\":\"a b b\"}"     // off 5
    );
    // 期望最终计数：a: 1+2+0+1+3+1=8? —— 以实际输入在 main 中独立计算期望，避免手抄错误

    public static void main(String[] args) throws Exception {
        // 以纯函数方式独立计算期望值（不走被测引擎）
        Map<String, Long> expectedCounts = new java.util.TreeMap<>();
        for (String rec : INPUTS) {
            String text = (String) Json.parseObject(rec).get("text");
            for (String w : text.trim().split("\\s+")) {
                expectedCounts.merge(w, 1L, Long::sum);
            }
        }

        String[] scenarios = {
                "NONE",
                "AFTER_PROCESS",
                "AFTER_STATE_PERSIST",
                "AFTER_OUTPUT_PREPARE",
                "AFTER_COMMIT"
        };

        for (String fp : scenarios) {
            System.out.println();
            System.out.println("########## 场景：failPoint=" + fp + " ##########");
            runScenario(fp, expectedCounts);
        }

        System.out.println();
        System.out.println("########## 连续多次崩溃（同一数据目录，不同故障点） ##########");
        runRepeatedCrashes(expectedCounts);

        System.exit(Assert.summary());
    }

    static void runScenario(String failPoint, Map<String, Long> expectedCounts) throws Exception {
        Path dataDir = Files.createTempDirectory("txflow-crash-" + failPoint + "-");
        int port = freePort();

        Process p = startServer(port, dataDir);
        try {
            waitForHealth(port, p);
            for (String rec : INPUTS) {
                post(port, "/inputs", rec);
            }
            // 基线：offset 0、offset 1 分两批提交
            post(port, "/process", "{\"maxRecords\":1}");
            post(port, "/process", "{\"maxRecords\":1}");

            if ("NONE".equals(failPoint)) {
                // 对照组：一次处理完剩余 4 条，无故障
                post(port, "/process", "{\"maxRecords\":10}");
            } else {
                // 崩溃批：offset 2,3
                boolean halted = callProcessExpectHalt(port,
                        "{\"maxRecords\":2,\"failPoint\":\"" + failPoint + "\"}", p);
                Assert.check(failPoint + "：服务进程在故障点硬终止", halted);
            }
        } finally {
            p.destroyForcibly();
            p.waitFor();
        }

        // 崩溃后、重启前的中间态观察（仅记录，不强约束——不同故障点窗口不同）
        inspectIntermediateState(failPoint, dataDir);

        // 用同一数据目录重启（自动恢复），处理完所有剩余记录
        Process p2 = startServer(port, dataDir);
        try {
            waitForHealth(port, p2);
            post(port, "/recover", "{}"); // 显式再跑一次恢复，必须幂等
            post(port, "/process", "{\"maxRecords\":10}");

            verifySink(port, failPoint, expectedCounts, 6);
        } finally {
            p2.destroyForcibly();
            p2.waitFor();
        }

        // 第三次启动：空跑，结果必须保持稳定
        Process p3 = startServer(port, dataDir);
        try {
            waitForHealth(port, p3);
            post(port, "/process", "{\"maxRecords\":10}");
            verifySink(port, failPoint + "（二次重启后）", expectedCounts, 6);
        } finally {
            p3.destroyForcibly();
            p3.waitFor();
        }
    }

    /**
     * 连续崩溃：offset 2 起逐条处理，每个剩余 offset 都在不同故障点崩溃一次，
     * 反复重启。压力验证协议在崩溃重放下的收敛性。
     */
    static void runRepeatedCrashes(Map<String, Long> expectedCounts) throws Exception {
        Path dataDir = Files.createTempDirectory("txflow-crash-repeat-");
        int port = freePort();
        String[] cycle = {"AFTER_PROCESS", "AFTER_STATE_PERSIST", "AFTER_OUTPUT_PREPARE", "AFTER_COMMIT"};

        Process p = startServer(port, dataDir);
        try {
            waitForHealth(port, p);
            for (String rec : INPUTS) {
                post(port, "/inputs", rec);
            }
        } finally {
            p.destroyForcibly();
            p.waitFor();
        }

        int attempt = 0;
        // 每条记录最多尝试 3 次（崩溃+重启），直到 6 条全部可见
        Set<Long> visible = new TreeSet<>();
        for (int safety = 0; safety < 40 && visible.size() < 6; safety++) {
            Process px = startServer(port, dataDir);
            try {
                waitForHealth(port, px);
                String fp = cycle[attempt % cycle.length];
                attempt++;
                boolean halted = callProcessExpectHalt(port,
                        "{\"maxRecords\":1,\"failPoint\":\"" + fp + "\"}", px);
                if (!halted) {
                    // 无崩溃 = 这批提交成功了
                }
            } finally {
                px.destroyForcibly();
                px.waitFor();
            }
            // 重启（下一轮循环），先看一眼当前可见集合
            Process peek = startServer(port, dataDir);
            try {
                waitForHealth(port, peek);
                visible = visibleOffsets(port);
            } finally {
                peek.destroyForcibly();
                peek.waitFor();
            }
        }

        Process finalP = startServer(port, dataDir);
        try {
            waitForHealth(port, finalP);
            post(port, "/process", "{\"maxRecords\":10}");
            verifySink(port, "连续崩溃", expectedCounts, 6);
            Assert.check("连续崩溃经历多次故障注入", attempt >= 4);
        } finally {
            finalP.destroyForcibly();
            finalP.waitFor();
        }
    }

    // ---------- 断言核对 ----------

    static void verifySink(int port, String label, Map<String, Long> expectedCounts, int expectedN)
            throws Exception {
        Map<String, Object> state = getJson(port, "/state");
        List<?> lines = (List<?>) ((Map<?, ?>) getJson(port, "/outputs")).get("lines");
        List<Long> offsets = new ArrayList<>();
        Set<Long> unique = new HashSet<>();
        Map<String, Object> lastState = null;
        for (Object o : lines) {
            Map<String, Object> outLine = Json.parseObject((String) o);
            long off = ((Number) outLine.get("inputOffset")).longValue();
            offsets.add(off);
            unique.add(off);
            lastState = outLine;
        }

        Assert.equals(label + "：可见输出条数", expectedN, lines.size());
        Assert.equals(label + "：无重复（offset 去重后大小）", expectedN, unique.size());

        Set<Long> expectedOffsets = new TreeSet<>();
        for (long i = 0; i < expectedN; i++) {
            expectedOffsets.add(i);
        }
        Assert.equals(label + "：无遗漏（offset 集合恰为 0..N-1）", expectedOffsets, unique);

        List<Long> sorted = new ArrayList<>(new TreeSet<>(offsets));
        Assert.equals(label + "：顺序严格递增", sorted, offsets);

        @SuppressWarnings("unchecked")
        Map<String, Object> finalCounts = (Map<String, Object>)
                ((Map<String, Object>) lastState.get("stateAfter")).get("counts");
        for (Map.Entry<String, Long> e : expectedCounts.entrySet()) {
            Assert.equals(label + "：最终计数 " + e.getKey(),
                    e.getValue(), ((Number) finalCounts.get(e.getKey())).longValue());
        }

        Assert.equals(label + "：快照 lastCommittedOffset", expectedN - 1L,
                ((Number) state.get("lastCommittedOffset")).longValue());
        Assert.equals(label + "：快照无悬挂 prepared", false, state.get("hasPrepared"));

        // committed.log 条数与输出一致（用 /markers 观察）
        List<?> markers = (List<?>) ((Map<?, ?>) getJson(port, "/markers")).get("markers");
        long lastEndOffset = ((Number) ((Map<?, ?>) markers.get(markers.size() - 1)).get("endOffset"))
                .longValue();
        Assert.equals(label + "：最后提交标记 endOffset", expectedN - 1L, lastEndOffset);
    }

    /** 崩溃后、重启前观察磁盘中间态（记录用；不同故障点应呈现不同窗口）。 */
    static void inspectIntermediateState(String fp, Path dataDir) throws Exception {
        Path cp = dataDir.resolve("checkpoint.json");
        Path markerLog = dataDir.resolve("sink/committed.log");
        String cpText = Files.exists(cp) ? Files.readString(cp, StandardCharsets.UTF_8) : "";
        boolean hasPrepared = cpText.contains("\"prepared\"") && !cpText.contains("\"prepared\": null");
        long markers = Files.exists(markerLog)
                ? Files.readString(markerLog, StandardCharsets.UTF_8).lines().filter(l -> !l.isBlank()).count()
                : 0;
        long outputFiles = Files.list(dataDir.resolve("sink/outputs")).count();
        System.out.println("  [中间态] " + fp + " prepared=" + hasPrepared
                + " committedMarkers=" + markers + " outputsOnDisk=" + outputFiles);

        switch (fp) {
            case "AFTER_PROCESS":
                // 处理完但没落盘：崩溃批无标记、无新输出文件
                Assert.equals(fp + " 中间态：提交标记仍为基线 2 条", 2L, markers);
                Assert.equals(fp + " 中间态：输出文件仍为基线 2 个", 2L, outputFiles);
                break;
            case "AFTER_STATE_PERSIST":
            case "AFTER_OUTPUT_PREPARE":
                // 快照含 prepared、输出文件已发布但无标记
                Assert.check(fp + " 中间态：快照含 prepared", hasPrepared);
                Assert.equals(fp + " 中间态：提交标记仍为 2 条", 2L, markers);
                Assert.equals(fp + " 中间态：输出文件 3 个（含未提交）", 3L, outputFiles);
                break;
            case "AFTER_COMMIT":
                Assert.check(fp + " 中间态：快照含 prepared", hasPrepared);
                Assert.equals(fp + " 中间态：提交标记已 3 条", 3L, markers);
                Assert.equals(fp + " 中间态：输出文件 3 个", 3L, outputFiles);
                break;
            default:
                break;
        }
    }

    // ---------- 进程与 HTTP 工具 ----------

    static Process startServer(int port, Path dataDir) throws Exception {
        ProcessBuilder pb = new ProcessBuilder(
                JAVA, "-cp", System.getProperty("java.class.path"),
                "com.example.txflow.HttpApiServer");
        pb.environment().put("TXFLOW_HOST", "127.0.0.1");
        pb.environment().put("TXFLOW_PORT", String.valueOf(port));
        pb.environment().put("TXFLOW_DATA", dataDir.toString());
        pb.redirectErrorStream(true);
        Path logFile = dataDir.resolve("server-" + System.nanoTime() + ".log");
        pb.redirectOutput(ProcessBuilder.Redirect.appendTo(logFile.toFile()));
        return pb.start();
    }

    static void waitForHealth(int port, Process p) throws Exception {
        long deadline = System.currentTimeMillis() + 15_000;
        while (System.currentTimeMillis() < deadline) {
            if (!p.isAlive()) {
                throw new IllegalStateException("服务进程提前退出, exit=" + p.exitValue());
            }
            try {
                HttpResponse<String> r = httpClient()
                        .send(HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + "/health"))
                                .timeout(Duration.ofSeconds(1)).GET().build(),
                                HttpResponse.BodyHandlers.ofString());
                if (r.statusCode() == 200) {
                    return;
                }
            } catch (Exception e) {
                Thread.sleep(100);
            }
        }
        throw new IllegalStateException("服务在 15s 内未就绪");
    }

    static boolean callProcessExpectHalt(int port, String jsonBody, Process p) throws Exception {
        try {
            post(port, "/process", jsonBody);
            // 没有崩溃（故障点 NONE 或未命中）
            return false;
        } catch (Exception connectionDropped) {
            // 对端 halt 导致连接中断——预期路径
        }
        long deadline = System.currentTimeMillis() + 10_000;
        while (p.isAlive() && System.currentTimeMillis() < deadline) {
            Thread.sleep(50);
        }
        if (p.isAlive()) {
            return false;
        }
        Assert.equals("halt 退出码为 17", 17, p.exitValue());
        return true;
    }

    static Set<Long> visibleOffsets(int port) throws Exception {
        List<?> lines = (List<?>) ((Map<?, ?>) getJson(port, "/outputs")).get("lines");
        Set<Long> set = new TreeSet<>();
        for (Object o : lines) {
            set.add(((Number) Json.parseObject((String) o).get("inputOffset")).longValue());
        }
        return set;
    }

    static String post(int port, String path, String json) throws Exception {
        HttpResponse<String> r = httpClient().send(
                HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path))
                        .header("Content-Type", "application/json")
                        .POST(HttpRequest.BodyPublishers.ofString(json, StandardCharsets.UTF_8))
                        .timeout(Duration.ofSeconds(10)).build(),
                HttpResponse.BodyHandlers.ofString());
        if (r.statusCode() / 100 != 2) {
            throw new IllegalStateException(path + " -> " + r.statusCode() + " " + r.body());
        }
        return r.body();
    }

    static Map<String, Object> getJson(int port, String path) throws Exception {
        HttpResponse<String> r = httpClient().send(
                HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path))
                        .timeout(Duration.ofSeconds(10)).GET().build(),
                HttpResponse.BodyHandlers.ofString());
        if (r.statusCode() / 100 != 2) {
            throw new IllegalStateException(path + " -> " + r.statusCode() + " " + r.body());
        }
        return Json.parseObject(r.body());
    }

    static HttpClient httpClient() {
        return HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
    }

    static int freePort() throws Exception {
        try (java.net.ServerSocket s = new java.net.ServerSocket(0)) {
            return s.getLocalPort();
        }
    }

    static String resolveJava() {
        String home = System.getProperty("java.home");
        Path java = Path.of(home, "bin", "java");
        return java.toString();
    }
}
