package phj;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * 命令行入口（JSON 请求 / JSON 响应）。
 *
 * 用法：
 *   java phj.Main [request.json]          # 文件参数；无参数时从标准输入读
 *   echo '{...}' | java phj.Main
 *
 * 响应写到标准输出；出错时响应体 ok=false，进程退出码：
 *   0 成功；2 请求非法；3 磁盘额度耗尽；1 其他内部错误。
 */
public final class Main {

    private Main() {}

    public static void main(String[] args) throws Exception {
        String requestJson;
        if (args.length == 0 || args[0].equals("-")) {
            requestJson = new String(System.in.readAllBytes(), StandardCharsets.UTF_8);
        } else {
            requestJson = Files.readString(Path.of(args[0]), StandardCharsets.UTF_8);
        }

        QueryRunner.Executed executed = QueryRunner.executeJson(requestJson);
        System.out.println(executed.json());
        if (executed.exitCode() != 0) System.exit(executed.exitCode());
    }
}
