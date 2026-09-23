package engine;

import engine.executor.RequestExecutor;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * JSON 请求入口（单机，无服务端、无前端）。
 *
 * <pre>
 *   java engine.Main [请求文件.json [输出文件.json]]
 * </pre>
 *
 * 不传参数时从标准输入读取请求；给一个参数时从文件读、结果写标准输出；
 * 给两个参数时额外把响应写入输出文件（数据/执行计划导出）。
 *
 * <p>退出码：0 成功；2 请求错误（JSON 语法/结构非法）；
 * 1 窗口计算错误（如整数溢出）；3 IO 错误。
 */
public final class Main {

    public static void main(String[] args) {
        String requestText;
        try {
            requestText = readRequest(args);
        } catch (IOException e) {
            System.err.println("读取请求失败: " + e.getMessage());
            System.exit(3);
            return;
        }
        RequestExecutor executor = new RequestExecutor();
        String response = executor.execute(requestText);
        System.out.println(response);

        if (args.length >= 2) {
            try {
                Path outPath = Path.of(args[1]);
                Path parent = outPath.toAbsolutePath().getParent();
                if (parent != null) {
                    Files.createDirectories(parent);
                }
                Files.writeString(outPath, response + System.lineSeparator(),
                        StandardCharsets.UTF_8);
            } catch (IOException e) {
                System.err.println("写出响应失败: " + e.getMessage());
                System.exit(3);
            }
        }

        if (executor.lastRequestFailed) {
            System.exit(executor.lastFailureWasWindowError ? 1 : 2);
        }
    }

    private static String readRequest(String[] args) throws IOException {
        if (args.length == 0) {
            return new String(System.in.readAllBytes(), StandardCharsets.UTF_8);
        }
        return Files.readString(Path.of(args[0]), StandardCharsets.UTF_8);
    }
}
