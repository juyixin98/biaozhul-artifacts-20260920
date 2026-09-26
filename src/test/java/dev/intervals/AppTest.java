package dev.intervals;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * App CLI 端到端测试：文件参数、标准输入、--demo 三条路径。
 * 直接调用 {@link App#run} 注入输入输出流，不触发 {@code System.exit}。
 */
class AppTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private JsonNode runOut(String[] args, String stdin) throws Exception {
        ByteArrayInputStream in =
                new ByteArrayInputStream(stdin.getBytes(StandardCharsets.UTF_8));
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        ByteArrayOutputStream err = new ByteArrayOutputStream();
        int code = App.run(args, in, new PrintStream(out), new PrintStream(err));
        assertEquals(0, code, "stderr: " + err);
        return MAPPER.readTree(out.toString().trim());
    }

    @Test
    @DisplayName("stdin：无参数从标准输入读取请求")
    void fromStdin() throws Exception {
        JsonNode resp = runOut(new String[0], """
                {"domain":"version","operation":"union",
                 "a":[{"lower":1,"upper":3}],"b":[{"lower":3,"upper":5}]}
                """);
        assertTrue(resp.get("ok").asBoolean());
        assertEquals(1, resp.get("result").size());
    }

    @Test
    @DisplayName("文件参数：从请求文件读取")
    void fromFile(@TempDir Path dir) throws Exception {
        Path req = dir.resolve("req.json");
        Files.writeString(req, """
                {"domain":"version","operation":"intersection",
                 "a":[{"lower":1,"upper":5}],"b":[{"lower":3,"upper":9}]}
                """);
        JsonNode resp = runOut(new String[] {req.toString()}, "");
        assertTrue(resp.get("ok").asBoolean());
        assertEquals(3, resp.get("result").get(0).get("lower").asInt());
    }

    @Test
    @DisplayName("--demo：内置固定数据可运行并返回四组结果")
    void demo() throws Exception {
        JsonNode resp = runOut(new String[] {"--demo"}, "");
        assertTrue(resp.get("ok").asBoolean());
        assertEquals(4, resp.get("results").size());
    }

    @Test
    @DisplayName("空输入返回退出码 2 且写入错误信息")
    void blankInputExits2() throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        ByteArrayOutputStream err = new ByteArrayOutputStream();
        int code = App.run(new String[0],
                new ByteArrayInputStream("  ".getBytes()),
                new PrintStream(out), new PrintStream(err));
        assertEquals(2, code);
        assertTrue(err.toString().contains("未提供请求内容"));
    }
}
