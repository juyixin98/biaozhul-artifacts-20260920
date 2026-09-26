package dev.intervals;

import dev.intervals.json.JsonService;

import java.io.IOException;
import java.io.InputStream;
import java.io.PrintStream;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * 命令行入口（纯后端，无前端）。
 *
 * <pre>
 *   java -jar interval-set-algebra-1.0.0.jar [请求文件.json]
 *   cat request.json | java -jar interval-set-algebra-1.0.0.jar
 *   java -jar interval-set-algebra-1.0.0.jar --demo
 * </pre>
 *
 * 无参数时从标准输入读取；{@code --demo} 使用内置固定测试数据。
 */
public final class App {

    static final String DEMO_REQUEST = """
            {
              "domain": "time",
              "timeZone": "Asia/Shanghai",
              "operation": "all",
              "a": [
                {"lower": "2026-01-01T00:00:00", "upper": "2026-03-31T23:59:59", "lowerOpen": false, "upperOpen": false},
                {"lower": "2026-03-31T23:59:59", "upper": "2026-06-30T00:00:00", "lowerOpen": true,  "upperOpen": true},
                {"lower": "2026-07-01T00:00:00", "upper": null}
              ],
              "b": [
                {"lower": "2026-02-01T00:00:00", "upper": "2026-02-14T23:59:59"},
                {"lower": "2026-03-31T23:59:59", "upper": "2026-03-31T23:59:59"}
              ]
            }
            """;

    private App() {
    }

    public static void main(String[] args) throws IOException {
        int code = run(args, System.in, System.out, System.err);
        if (code != 0) {
            System.exit(code);
        }
    }

    /**
     * 读取请求、执行运算并输出。返回进程退出码（0 成功，2 输入为空/不可读）。
     */
    static int run(String[] args, InputStream in, PrintStream out, PrintStream err)
            throws IOException {
        String request;
        if (args.length == 0) {
            request = new String(in.readAllBytes());
        } else if ("--demo".equals(args[0])) {
            request = DEMO_REQUEST;
        } else {
            request = Files.readString(Path.of(args[0]));
        }
        if (request == null || request.isBlank()) {
            err.println("错误: 未提供请求内容");
            return 2;
        }
        out.println(new JsonService().handle(request));
        return 0;
    }
}
