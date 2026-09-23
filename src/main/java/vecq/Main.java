package vecq;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 命令行入口。
 *
 * 用法：
 *   vecq run <request.json | ->            执行一次查询（- 表示从标准输入读 JSON）
 *   vecq serve [--port N] [table.json ...] 启动 HTTP 服务，可预加载表文件到目录
 *
 * 退出码：0 成功；1 请求非法 / 查询失败；2 用法或 IO 错误。
 */
public final class Main {

    public static void main(String[] args) {
        if (args.length == 0) {
            usage();
            System.exit(2);
        }
        try {
            switch (args[0]) {
                case "run" -> run(args);
                case "serve" -> serve(args);
                case "-h", "--help", "help" -> {
                    usage();
                    System.exit(0);
                }
                default -> {
                    System.err.println("未知命令: " + args[0]);
                    usage();
                    System.exit(2);
                }
            }
        } catch (SystemExit e) {
            System.exit(e.code);
        }
    }

    private static void run(String[] args) {
        if (args.length < 2) {
            System.err.println("用法: vecq run <request.json | ->");
            throw new SystemExit(2);
        }
        String text;
        try {
            text = "-".equals(args[1])
                    ? new String(System.in.readAllBytes(), java.nio.charset.StandardCharsets.UTF_8)
                    : Files.readString(Path.of(args[1]));
        } catch (IOException e) {
            System.err.println("读取请求文件失败: " + e.getMessage());
            throw new SystemExit(2);
        }
        Object parsed;
        try {
            parsed = Json.parse(text);
        } catch (Json.JsonException e) {
            System.err.println("JSON 解析失败: " + e.getMessage());
            throw new SystemExit(1);
        }
        QueryEngine engine = new QueryEngine(new Catalog());
        try {
            QueryResult result = engine.execute(parsed);
            Exporter.exportAll(result);
            System.out.println(Json.writePretty(result.toResponseJson()));
        } catch (InvalidQueryException | InvalidSelectionException | EngineMismatchException e) {
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("ok", false);
            err.put("error", e.getMessage());
            System.err.println(Json.writePretty(err));
            throw new SystemExit(1);
        }
    }

    private static void serve(String[] args) {
        int port = 8080;
        List<String> tableFiles = new ArrayList<>();
        for (int i = 1; i < args.length; i++) {
            if ("--port".equals(args[i])) {
                if (i + 1 >= args.length) {
                    System.err.println("--port 需要一个参数");
                    throw new SystemExit(2);
                }
                port = Integer.parseInt(args[++i]);
            } else {
                tableFiles.add(args[i]);
            }
        }
        Catalog catalog = new Catalog();
        for (String f : tableFiles) {
            try {
                Object t = Json.parse(Files.readString(Path.of(f)));
                catalog.register(Table.fromJson(t));
            } catch (IOException | Json.JsonException | InvalidQueryException e) {
                System.err.println("加载表文件失败 " + f + ": " + e.getMessage());
                throw new SystemExit(2);
            }
        }
        QueryEngine engine = new QueryEngine(catalog);
        VecqServer server = new VecqServer(engine);
        try {
            server.start(port);
        } catch (IOException e) {
            System.err.println("启动 HTTP 服务失败: " + e.getMessage());
            throw new SystemExit(2);
        }
        System.out.println("vecq HTTP 服务已启动: http://localhost:" + server.port()
                + "  （POST /query，GET /tables，GET /health；Ctrl+C 退出）");
        if (!tableFiles.isEmpty()) {
            System.out.println("已预加载表: " + catalog.tables().keySet());
        }
        // 阻塞直到被终止
        Runtime.getRuntime().addShutdownHook(new Thread(server::stop));
        try {
            Thread.currentThread().join();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    private static void usage() {
        System.err.println("""
                vecq —— 列式选择向量单机内存查询引擎

                用法:
                  vecq run <request.json | ->            执行一次查询（- 从标准输入读取）
                  vecq serve [--port N] [table.json ...] 启动 JSON HTTP 服务
                  vecq help

                示例:
                  vecq run examples/request_filter.json
                  vecq serve --port 8080 examples/table_orders.json
                  curl -s -X POST localhost:8080/query -d @examples/request_filter.json
                """);
    }

    private static final class SystemExit extends RuntimeException {
        final int code;
        SystemExit(int code) { super(null, null, false, false); this.code = code; }
    }
}
