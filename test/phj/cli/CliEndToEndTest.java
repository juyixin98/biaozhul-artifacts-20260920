package phj.cli;

import phj.Test;
import phj.TestRunner;
import phj.json.Json;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

/** 通过子进程调用 CLI，验证标准输入/文件入口、退出码与响应结构。 */
public class CliEndToEndTest {

    private static String classpath() {
        return System.getProperty("java.class.path");
    }

    private record Result(int exit, String stdout, String stderr) {}

    private static Result runCli(String inputFile, String stdin) throws Exception {
        Path cpFile = Files.createTempFile("phj-stdin-", ".json");
        try {
            List<String> cmd = new java.util.ArrayList<>();
            cmd.add(java.nio.file.Paths.get(System.getProperty("java.home"), "bin", "java").toString());
            cmd.add("-cp");
            cmd.add(classpath());
            cmd.add("phj.Main");
            if (inputFile != null) cmd.add(inputFile);
            ProcessBuilder pb = new ProcessBuilder(cmd);
            Process p = pb.start();
            if (stdin != null) {
                p.getOutputStream().write(stdin.getBytes(StandardCharsets.UTF_8));
                p.getOutputStream().close();
            }
            String out = new String(p.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
            String err = new String(p.getErrorStream().readAllBytes(), StandardCharsets.UTF_8);
            int code = p.waitFor();
            return new Result(code, out, err);
        } finally {
            Files.deleteIfExists(cpFile);
        }
    }

    private static String sampleRequest(String joinType, long threshold, long quota) {
        return """
            {
              "joinType": "%s",
              "keys": ["k"],
              "left":  {"name":"L","columns":["id","k"],"rows":[[1,"a"],[2,"b"],[3,null],[4,"a"]]},
              "right": {"name":"R","columns":["cid","k"],"rows":[[10,"a"],[11,"a"],[12,"c"]]},
              "options": {"memoryThresholdRows": %d, "partitions": 3, "diskQuotaBytes": %d}
            }
            """.formatted(joinType, threshold, quota);
    }

    @Test
    public void stdinInputExitZero() throws Exception {
        Result r = runCli(null, sampleRequest("INNER", 2, -1));
        TestRunner.assertEquals(0, r.exit());
        Map<String, Object> resp = Json.asObj(Json.parse(r.stdout()), "resp");
        TestRunner.assertEquals(Boolean.TRUE, resp.get("ok"));
        Map<?, ?> result = (Map<?, ?>) resp.get("result");
        // a:2x2=4
        TestRunner.assertEquals(4L, ((Number) result.get("rowCount")).longValue());
    }

    @Test
    public void fileInputLeftJoin() throws Exception {
        Path f = Files.createTempFile("phj-req-", ".json");
        Files.writeString(f, sampleRequest("LEFT", 1, -1));
        Result r = runCli(f.toString(), null);
        TestRunner.assertEquals(0, r.exit());
        Map<?, ?> resp = Json.asObj(Json.parse(r.stdout()), "resp");
        Map<?, ?> result = (Map<?, ?>) resp.get("result");
        // a 4 条 + b 补 NULL + null 补 NULL = 6
        TestRunner.assertEquals(6L, ((Number) result.get("rowCount")).longValue());
        Files.deleteIfExists(f);
    }

    @Test
    public void invalidJsonExit2() throws Exception {
        Result r = runCli(null, "{ not json");
        TestRunner.assertEquals(2, r.exit());
        Map<?, ?> resp = Json.asObj(Json.parse(r.stdout()), "resp");
        TestRunner.assertEquals(Boolean.FALSE, resp.get("ok"));
        TestRunner.assertEquals("INVALID_REQUEST", resp.get("errorType"));
    }

    @Test
    public void missingColumnExit2() throws Exception {
        String bad = """
                {
                  "joinType": "INNER",
                  "keys": ["nope"],
                  "left":  {"name":"L","columns":["id","k"],"rows":[[1,"a"]]},
                  "right": {"name":"R","columns":["cid","k"],"rows":[[10,"a"]]}
                }
                """;
        Result r = runCli(null, bad);
        TestRunner.assertEquals(2, r.exit());
        Map<?, ?> resp = Json.asObj(Json.parse(r.stdout()), "resp");
        TestRunner.assertEquals("INVALID_REQUEST", resp.get("errorType"));
    }

    @Test
    public void quotaExceededExit3() throws Exception {
        Result r = runCli(null, sampleRequest("INNER", 1, 0));
        TestRunner.assertEquals(3, r.exit());
        Map<?, ?> resp = Json.asObj(Json.parse(r.stdout()), "resp");
        TestRunner.assertEquals("DISK_QUOTA_EXCEEDED", resp.get("errorType"));
        TestRunner.assertEquals(0L, ((Number) resp.get("diskQuotaBytes")).longValue());
    }

    @Test
    public void planIsExportedByDefault() throws Exception {
        Result r = runCli(null, sampleRequest("INNER", 2, -1));
        Map<?, ?> resp = Json.asObj(Json.parse(r.stdout()), "resp");
        TestRunner.assertTrue(resp.containsKey("executionPlan"), "默认应导出执行计划");
        Map<?, ?> plan = (Map<?, ?>) resp.get("executionPlan");
        TestRunner.assertTrue(plan.containsKey("stats"));
        TestRunner.assertTrue(plan.containsKey("plan"));
    }
}
