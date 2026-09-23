package tvl;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import tvl.api.JsonApi;
import tvl.json.Json;
import tvl.server.ApiServer;

/**
 * 命令行入口。
 *
 * 用法：
 *   java -cp out tvl.Main serve [--port 8080]        启动 HTTP 服务（默认端口 8080）
 *   java -cp out tvl.Main run  <file.json>           执行一个或一批 JSON 请求后退出
 *   java -cp out tvl.Main stdin                      从标准输入读取 JSON 请求
 *
 * 单文件支持三种形态：
 *   1) 单个 JSON 对象            -> 返回单个 JSON 对象
 *   2) JSON 数组（多个请求对象） -> 顺序执行、共享内存状态，返回 JSON 数组
 *   3) JSON Lines（每行一个对象）-> 同上，返回 JSON 数组
 */
public final class Main {

    public static void main(String[] args) {
        if (args.length == 0) {
            printUsage();
            System.exit(2);
        }
        String command = args[0];
        int exitCode = switch (command) {
            case "serve" -> serve(args);
            case "run" -> runFile(args);
            case "stdin" -> runStdin();
            case "-h", "--help", "help" -> {
                printUsage();
                yield 0;
            }
            default -> {
                System.err.println("unknown command: " + command);
                printUsage();
                yield 2;
            }
        };
        System.exit(exitCode);
    }

    private static int serve(String[] args) {
        int port = 8080;
        for (int i = 1; i < args.length; i++) {
            if ("--port".equals(args[i]) && i + 1 < args.length) {
                port = Integer.parseInt(args[++i]);
            }
        }
        try {
            ApiServer server = new ApiServer(port);
            server.start();
            System.out.println("tvl query engine listening on http://localhost:" + port);
            System.out.println("POST JSON to http://localhost:" + port + "/query ; Ctrl+C to stop");
            // 阻塞直到进程被终止
            Thread.currentThread().join();
            return 0;
        } catch (NumberFormatException ex) {
            System.err.println("invalid port number");
            return 2;
        } catch (IOException ex) {
            System.err.println("failed to start server: " + ex.getMessage());
            return 1;
        } catch (InterruptedException ex) {
            Thread.currentThread().interrupt();
            return 0;
        }
    }

    private static int runFile(String[] args) {
        if (args.length < 2) {
            System.err.println("usage: run <file.json>");
            return 2;
        }
        Path path = Path.of(args[1]);
        if (!Files.isReadable(path)) {
            System.err.println("cannot read file: " + path);
            return 2;
        }
        String content;
        try {
            content = Files.readString(path, StandardCharsets.UTF_8);
        } catch (IOException ex) {
            System.err.println("failed to read file: " + ex.getMessage());
            return 1;
        }
        String output = executeInput(content);
        System.out.println(Json.writePretty(Json.parse(output)));
        return output.contains("\"ok\":false") ? 1 : 0;
    }

    private static int runStdin() {
        String content;
        try {
            content = new String(System.in.readAllBytes(), StandardCharsets.UTF_8);
        } catch (IOException ex) {
            System.err.println("failed to read stdin: " + ex.getMessage());
            return 1;
        }
        String output = executeInput(content);
        System.out.println(Json.writePretty(Json.parse(output)));
        return output.contains("\"ok\":false") ? 1 : 0;
    }

    /** 支持单对象 / JSON 数组 / 任意拼接的对象流（美化多行亦可）；请求共享同一引擎状态。 */
    @SuppressWarnings("unchecked")
    private static String executeInput(String content) {
        String trimmed = content.trim();
        if (trimmed.isEmpty()) {
            return Json.write(errorEnvelope("INVALID_REQUEST", "empty input"));
        }

        JsonApi api = new JsonApi();

        // 先尝试把整个输入解析为一个 JSON 值：单个对象或对象数组
        try {
            Object whole = Json.parse(trimmed);
            if (whole instanceof Map<?, ?> map) {
                return Json.write(api.handle((Map<String, Object>) map));
            }
            if (whole instanceof List<?> list) {
                return Json.write(runBatch(api, list));
            }
        } catch (RuntimeException ignored) {
            // 落到下面的对象流切分
        }

        // 多个对象的拼接（JSON Lines 或多个美化对象首尾相接）：按括号配对切分
        List<Object> responses = new ArrayList<>();
        int index = 0;
        int n = trimmed.length();
        while (index < n) {
            while (index < n && Character.isWhitespace(trimmed.charAt(index))) index++;
            if (index >= n) break;
            if (trimmed.charAt(index) != '{') {
                return Json.write(errorEnvelope("INVALID_JSON",
                        "expected a JSON object at element " + (responses.size() + 1)));
            }
            int end = findObjectEnd(trimmed, index);
            if (end < 0) {
                return Json.write(errorEnvelope("INVALID_JSON",
                        "unterminated JSON object at element " + (responses.size() + 1)));
            }
            Object parsed;
            try {
                parsed = Json.parse(trimmed.substring(index, end));
            } catch (RuntimeException ex) {
                return Json.write(errorEnvelope("INVALID_JSON",
                        "element " + (responses.size() + 1) + ": " + ex.getMessage()));
            }
            if (!(parsed instanceof Map<?, ?>)) {
                return Json.write(errorEnvelope("INVALID_REQUEST",
                        "element " + (responses.size() + 1) + " is not a JSON object"));
            }
            responses.add(api.handle((Map<String, Object>) parsed));
            index = end;
        }
        if (responses.isEmpty()) {
            return Json.write(errorEnvelope("INVALID_JSON", "input is not valid JSON"));
        }
        return Json.write(responses);
    }

    @SuppressWarnings("unchecked")
    private static List<Object> runBatch(JsonApi api, List<?> list) {
        List<Object> responses = new ArrayList<>();
        for (Object item : list) {
            if (!(item instanceof Map<?, ?>)) {
                throw new IllegalArgumentException("batch request must contain only JSON objects");
            }
            responses.add(api.handle((Map<String, Object>) item));
        }
        return responses;
    }

    /**
     * 从一个 '{' 的位置开始，按字符串/转义感知的括号配对找到对应对象末尾（含 '}'）。
     * @return 结束位置（exclusive）；括号不闭合返回 -1
     */
    private static int findObjectEnd(String text, int start) {
        int depth = 0;
        boolean inString = false;
        boolean escaped = false;
        for (int i = start; i < text.length(); i++) {
            char c = text.charAt(i);
            if (inString) {
                if (escaped) {
                    escaped = false;
                } else if (c == '\\') {
                    escaped = true;
                } else if (c == '"') {
                    inString = false;
                }
                continue;
            }
            switch (c) {
                case '"' -> inString = true;
                case '{', '[' -> depth++;
                case '}', ']' -> {
                    depth--;
                    if (depth == 0) return i + 1;
                }
                default -> {
                }
            }
        }
        return -1;
    }

    private static Map<String, Object> errorEnvelope(String code, String message) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("ok", false);
        m.put("error", code);
        m.put("message", message);
        return m;
    }

    private static void printUsage() {
        System.out.println("""
                Three-valued-logic in-memory query engine

                Usage:
                  tvl serve [--port 8080]      Start HTTP server (POST /query)
                  tvl run <file.json>          Execute a JSON request / batch, then exit
                  tvl stdin                    Read a JSON request from stdin

                Request file may be a JSON object, a JSON array of requests,
                or JSON Lines (one request object per line). Requests in a batch
                share the same in-memory tables (load first, then query/export).
                """);
    }
}
