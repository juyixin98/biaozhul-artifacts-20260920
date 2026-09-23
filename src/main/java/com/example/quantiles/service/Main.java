package com.example.quantiles.service;

import com.example.quantiles.json.Json;
import com.example.quantiles.json.JsonParser;
import com.example.quantiles.json.JsonWriter;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;

/**
 * 命令行入口（纯后端，无前端）。
 *
 * <pre>
 *   java com.example.quantiles.service.Main batch <request.json>
 *       离线计算并把 JSON 响应打印到标准输出
 *
 *   java com.example.quantiles.service.Main serve [--port=8080]
 *       启动 HTTP 服务（端口默认 8080，可用 PORT 环境变量覆盖）
 * </pre>
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        if (args.length == 0) {
            usageAndExit();
        }
        switch (args[0]) {
            case "batch" -> runBatch(args);
            case "serve" -> runServe(args);
            case "-h", "--help", "help" -> {
                printUsage();
            }
            default -> {
                System.err.println("未知命令: " + args[0]);
                usageAndExit();
            }
        }
    }

    private static void runBatch(String[] args) throws IOException {
        if (args.length < 2) {
            System.err.println("batch 需要一个请求文件路径参数");
            usageAndExit();
        }
        Path path = Path.of(args[1]);
        if (!Files.isRegularFile(path)) {
            System.err.println("找不到请求文件: " + path);
            System.exit(2);
        }
        String text = Files.readString(path, StandardCharsets.UTF_8);
        Object parsed = JsonParser.parse(text);
        Map<String, Object> request = Json.asObject(parsed);
        Map<String, Object> response = new BatchQuantileService().handle(request);
        System.out.println(JsonWriter.writePretty(response));
    }

    private static void runServe(String[] args) throws Exception {
        int port = 8080;
        String envPort = System.getenv("PORT");
        if (envPort != null && !envPort.isBlank()) {
            port = Integer.parseInt(envPort);
        }
        for (String arg : args) {
            if (arg.startsWith("--port=")) {
                port = Integer.parseInt(arg.substring("--port=".length()));
            }
        }
        QuantileHttpServer http = new QuantileHttpServer(port);
        http.start();
        System.out.println("滑动窗口精确分位数服务已启动: http://localhost:" + http.getPort());
        System.out.println("  GET  /health");
        System.out.println("  POST /quantiles");
        Runtime.getRuntime().addShutdownHook(new Thread(() -> http.stop(1)));
        // 阻塞直到进程被终止
        Thread.currentThread().join();
    }

    private static void usageAndExit() {
        printUsage();
        System.exit(2);
    }

    private static void printUsage() {
        System.out.println("""
                用法:
                  java com.example.quantiles.service.Main batch <request.json>
                      离线计算滑动窗口精确分位数，结果 JSON 输出到 stdout
                  java com.example.quantiles.service.Main serve [--port=8080]
                      启动 JSON HTTP 服务（GET /health, POST /quantiles）
                """);
    }
}
