package com.example.cdc;

import java.nio.file.Path;
import java.nio.file.Paths;

/**
 * 服务入口。
 *
 * <p>用法：
 * <pre>
 *   java com.example.cdc.Main [port] [dataDir]
 *   环境变量 CDC_PORT / CDC_DATA_DIR 同样可配置；默认 8080 / ./data
 * </pre>
 * 启动时打开 WAL（自动修复崩溃残行）并重放重建全部状态。
 */
public final class Main {

    public static void main(String[] args) {
        int port = Integer.parseInt(getConfig(args, 0, "CDC_PORT", "8080"));
        Path dataDir = Paths.get(getConfig(args, 1, "CDC_DATA_DIR", "data"));
        Path walPath = dataDir.resolve("cdc.wal");

        Wal wal = Wal.open(walPath);
        Engine engine = new Engine(wal);
        engine.recover();
        ApiServer api = new ApiServer(port, engine, wal);
        api.start();

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            api.stop();
            wal.close();
        }));

        System.out.println("变更流状态重建服务已启动");
        System.out.println("  监听端口: " + api.port());
        System.out.println("  WAL 文件: " + walPath.toAbsolutePath());
        System.out.println("  " + engine.status());
        System.out.println("接口样例见 README.md");
    }

    private static String getConfig(String[] args, int idx, String env, String def) {
        if (args.length > idx && !args[idx].isBlank()) {
            return args[idx];
        }
        String v = System.getenv(env);
        return v != null && !v.isBlank() ? v : def;
    }

    private Main() {
    }
}
