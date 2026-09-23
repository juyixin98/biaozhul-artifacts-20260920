package windowengine;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 端到端测试：真正启动子进程执行 java windowengine.Main，
 * 验证 stdin/文件两种入口、退出码、stdout JSON 内容以及 --export 落盘效果。
 * 运行前提：测试 classpath 同时包含 build/classes 与 build/test-classes，
 * 由 scripts/run_tests.sh 保证。
 */
public final class EndToEndTest {

    private record ProcResult(int exitCode, String stdout, String stderr) {
    }

    private static ProcResult runMain(List<String> args, String stdin) throws Exception {
        String cp = System.getProperty("java.class.path");
        List<String> cmd = new java.util.ArrayList<>();
        String javaBin = Path.of(System.getProperty("java.home"), "bin", "java").toString();
        cmd.add(javaBin);
        cmd.add("-cp");
        cmd.add(cp);
        cmd.add("windowengine.Main");
        cmd.addAll(args);

        ProcessBuilder pb = new ProcessBuilder(cmd);
        Process p = pb.start();
        if (stdin != null) {
            p.getOutputStream().write(stdin.getBytes(StandardCharsets.UTF_8));
            p.getOutputStream().close();
        }
        String out = new String(p.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
        String err = new String(p.getErrorStream().readAllBytes(), StandardCharsets.UTF_8);
        int code = p.waitFor();
        return new ProcResult(code, out, err);
    }

    private String sampleRequestJson() {
        return """
                {
                  "data": {
                    "columns": [
                      {"name": "dept", "type": "STRING"},
                      {"name": "v", "type": "LONG"}
                    ],
                    "rows": [
                      ["A", 1],
                      ["A", 2],
                      ["B", 10],
                      ["A", null]
                    ]
                  },
                  "plan": {
                    "window": {
                      "partitionBy": ["dept"],
                      "orderBy": [{"column": "v", "direction": "ASC", "nullOrder": "FIRST"}],
                      "frame": "ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING"
                    },
                    "functions": [
                      {"function": "ROW_NUMBER", "alias": "rn"},
                      {"function": "RANK", "alias": "rk"},
                      {"function": "SUM", "column": "v", "alias": "s"}
                    ]
                  }
                }
                """;
    }

    public void testStdinSuccess() throws Exception {
        ProcResult r = runMain(List.of(), sampleRequestJson());
        Asserts.assertEquals(0, r.exitCode, "成功退出码应为 0");
        Object parsed = Json.parse(r.stdout);
        @SuppressWarnings("unchecked")
        Map<String, Object> resp = (Map<String, Object>) parsed;
        Asserts.assertEquals(Boolean.TRUE, resp.get("ok"), "响应 ok=true");
        Asserts.assertEquals(4L, ((Number) resp.get("rowCount")).longValue(), "4 行结果");
        Asserts.assertTrue(r.stdout.contains("\"rn\""), "输出含 rn 列");
        Asserts.assertTrue(r.stdout.contains("\"s\""), "输出含 s 列");
    }

    public void testFileArgumentSuccess() throws Exception {
        Path reqFile = Files.createTempFile("window-req", ".json");
        Files.writeString(reqFile, sampleRequestJson(), StandardCharsets.UTF_8);
        ProcResult r = runMain(List.of(reqFile.toString()), null);
        Asserts.assertEquals(0, r.exitCode, "文件入口成功");
        // 只匹配不依赖美化分隔符的稳定子串
        Asserts.assertTrue(r.stdout.contains("\"ok\""), "文件入口响应含 ok 字段");
        Object parsed = Json.parse(r.stdout);
        @SuppressWarnings("unchecked")
        Map<String, Object> resp2 = (Map<String, Object>) parsed;
        Asserts.assertEquals(Boolean.TRUE, resp2.get("ok"), "文件入口 ok=true");
        Files.deleteIfExists(reqFile);
    }

    public void testErrorExitCodeAndPayload() throws Exception {
        // 引用不存在的分区列（只改 partitionBy，schema 里的 dept 保持不变）
        String bad = sampleRequestJson().replace(
                "\"partitionBy\": [\"dept\"]", "\"partitionBy\": [\"ghost\"]");
        ProcResult r = runMain(List.of(), bad);
        Asserts.assertEquals(2, r.exitCode, "请求错误退出码应为 2");
        Object parsed = Json.parse(r.stdout);
        @SuppressWarnings("unchecked")
        Map<String, Object> resp = (Map<String, Object>) parsed;
        Asserts.assertEquals(Boolean.FALSE, resp.get("ok"), "ok=false");
        @SuppressWarnings("unchecked")
        Map<String, Object> err = (Map<String, Object>) resp.get("error");
        Asserts.assertEquals(ErrorCode.COLUMN_NOT_FOUND.code(), err.get("code"),
                "错误码透传");
    }

    public void testOverflowThroughCli() throws Exception {
        String req = """
                {
                  "data": {"columns": ["v"], "rows": [[9223372036854775807], [1]]},
                  "plan": {
                    "window": {"orderBy": [{"column": "v"}],
                      "frame": "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING"},
                    "functions": [{"function": "SUM", "column": "v", "alias": "s"}]
                  }
                }
                """;
        ProcResult r = runMain(List.of(), req);
        Asserts.assertEquals(2, r.exitCode, "溢出应非零退出");
        Asserts.assertTrue(r.stdout.contains("OVERFLOW"), "溢出错误码出现在输出: " + r.stdout);
    }

    public void testExportWritesFiles() throws Exception {
        Path exportDir = Path.of("build", "e2e-export");
        deleteDir(exportDir);
        String withExport = sampleRequestJson().replaceFirst(
                "\"plan\"",
                "\"export\": \"" + exportDir.toString().replace("\\", "\\\\") + "\",\n  \"plan\"");
        ProcResult r = runMain(List.of(), withExport);
        Asserts.assertEquals(0, r.exitCode, "带导出的请求成功: " + r.stderr);
        Asserts.assertTrue(Files.exists(exportDir.resolve("data.json")), "data.json 落盘");
        Asserts.assertTrue(Files.exists(exportDir.resolve("plan.json")), "plan.json 落盘");
        Asserts.assertTrue(Files.exists(exportDir.resolve("request.json")), "request.json 落盘");

        // data.json 必须可回读且结构与响应一致
        Object dataNode = Json.parseFile(exportDir.resolve("data.json"));
        @SuppressWarnings("unchecked")
        Map<String, Object> data = (Map<String, Object>) dataNode;
        @SuppressWarnings("unchecked")
        List<Object> rows = (List<Object>) data.get("rows");
        Asserts.assertEquals(4, rows.size(), "导出数据 4 行");
        deleteDir(exportDir);
    }

    public void testFrameTextViaCli() throws Exception {
        // 验证 SQL 风格帧文本在真实 CLI 链路可用，且大偏移空帧返回 null
        String req = """
                {
                  "data": {"columns": ["v"], "rows": [[1],[2],[3]]},
                  "plan": {
                    "window": {"orderBy": [{"column": "v"}],
                      "frame": "ROWS BETWEEN 5 FOLLOWING AND 9 FOLLOWING"},
                    "functions": [{"function": "SUM", "column": "v", "alias": "s"}]
                  }
                }
                """;
        ProcResult r = runMain(List.of(), req);
        Asserts.assertEquals(0, r.exitCode, "空帧请求成功");
        // 三行的 s 都应是 null
        int nullCount = 0;
        int idx = 0;
        while ((idx = r.stdout.indexOf("null", idx)) >= 0) {
            nullCount++;
            idx += 4;
        }
        Asserts.assertTrue(nullCount >= 3, "至少 3 个 null（三个空帧结果），实际 " + nullCount);
    }

    private static void deleteDir(Path dir) throws Exception {
        if (!Files.exists(dir)) {
            return;
        }
        try (var walk = Files.walk(dir)) {
            walk.sorted(java.util.Comparator.reverseOrder()).forEach(p -> {
                try {
                    Files.deleteIfExists(p);
                } catch (Exception ignored) {
                    // 清理尽力而为
                }
            });
        }
    }
}
