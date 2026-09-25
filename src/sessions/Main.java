package sessions;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;

import sessions.json.Json;
import sessions.json.JsonWriter;
import sessions.service.SessionHttpServer;
import sessions.service.SessionRequestRunner;

/**
 * 命令行入口：
 *
 * <pre>
 *   java sessions.Main serve [port]            启动 HTTP 服务（默认 8080）
 *   java sessions.Main run <request.json>      离线运行一个请求文件并打印 JSON
 *   java sessions.Main run -                   从标准输入读取请求 JSON
 * </pre>
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        if (args.length == 0) {
            usage();
            System.exit(2);
        }
        switch (args[0]) {
            case "serve" -> serve(args.length > 1 ? Integer.parseInt(args[1]) : 8080);
            case "run" -> run(args.length > 1 ? args[1] : "-");
            default -> {
                usage();
                System.exit(2);
            }
        }
    }

    private static void serve(int port) throws IOException {
        SessionHttpServer httpServer = SessionHttpServer.start(port);
        System.out.println("event-time session-window service listening on port "
                + httpServer.port());
        System.out.println("  GET  /health");
        System.out.println("  POST /sessions/run");
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            httpServer.stop();
            System.out.println("server stopped");
        }));
        // 服务模式常驻，直到进程被终止。
        try {
            Thread.currentThread().join();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    @SuppressWarnings("unchecked")
    private static void run(String location) throws IOException {
        String text;
        if ("-".equals(location)) {
            text = new String(System.in.readAllBytes(), StandardCharsets.UTF_8);
        } else {
            text = Files.readString(Path.of(location), StandardCharsets.UTF_8);
        }
        Object parsed = Json.parse(text);
        if (!(parsed instanceof Map<?, ?>)) {
            System.err.println("request must be a JSON object");
            System.exit(1);
        }
        Object response = SessionRequestRunner.run((Map<String, Object>) parsed);
        System.out.println(JsonWriter.writePretty(response));
    }

    private static void usage() {
        System.err.println("""
                usage:
                  java sessions.Main serve [port]
                  java sessions.Main run <request.json | ->

                example:
                  java sessions.Main run samples/01-bridge-unordered.json
                  curl -s -X POST localhost:8080/sessions/run \\
                       -d @samples/01-bridge-unordered.json""");
    }
}
