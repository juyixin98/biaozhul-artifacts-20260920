package com.example.bitemporal.cli;

import com.example.bitemporal.engine.BitemporalException;
import com.example.bitemporal.json.RequestService;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 纯后端命令行入口：读取一个 JSON 请求，输出一个 JSON 响应信封。
 *
 * <ul>
 *   <li>{@code java -jar ... file.json}：从文件读取请求；</li>
 *   <li>无参数：从标准输入读取（适合管道）。</li>
 * </ul>
 *
 * <p>响应信封：成功为 {@code {"ok":true, ...操作结果}}，
 * 失败为 {@code {"ok":false,"error":{...}}}（进程退出码 1）。
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) {
        ObjectMapper mapper = new ObjectMapper()
                .registerModule(new JavaTimeModule())
                .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS)
                .enable(SerializationFeature.INDENT_OUTPUT);

        try {
            String json = readInput(args);
            Object requestNode = mapper.readValue(json, Object.class);
            Map<String, Object> result = new RequestService().handle(
                    mapper.valueToTree(requestNode));
            System.out.println(mapper.writeValueAsString(result));
        } catch (BitemporalException e) {
            emitError(mapper, e.getMessage());
            System.exit(1);
        } catch (IOException e) {
            emitError(mapper, "malformed JSON: " + e.getMessage());
            System.exit(1);
        } catch (RuntimeException e) {
            emitError(mapper, "unexpected error: " + e.getClass().getSimpleName()
                    + ": " + e.getMessage());
            System.exit(1);
        }
    }

    private static String readInput(String[] args) throws IOException {
        if (args.length > 0) {
            return Files.readString(Path.of(args[0]), StandardCharsets.UTF_8);
        }
        return new String(System.in.readAllBytes(), StandardCharsets.UTF_8);
    }

    private static void emitError(ObjectMapper mapper, String message) {
        try {
            Map<String, Object> envelope = new LinkedHashMap<>();
            envelope.put("ok", false);
            envelope.put("error", Map.of("message", message));
            System.out.println(mapper.writeValueAsString(envelope));
        } catch (IOException ignored) {
            System.out.println("{\"ok\":false,\"error\":{\"message\":\""
                    + message.replace("\"", "'") + "\"}}");
        }
    }
}
