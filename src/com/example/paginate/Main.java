package com.example.paginate;

import com.example.paginate.cursor.CursorService;
import com.example.paginate.web.HttpServerApp;

import java.time.Duration;

/**
 * 启动入口。
 *
 * 环境变量（均有默认值）：
 *   PORT            监听端口，默认 8080（测试时用 0 由系统分配）
 *   SNAPSHOT_TTL    快照 TTL 秒数，默认 300
 *   CURSOR_SECRET   游标 HMAC 密钥；不提供则每次启动随机生成（重启后旧游标失效）
 */
public final class Main {

    public static void main(String[] args) throws Exception {
        int port = envInt("PORT", 8080);
        long ttlSeconds = envInt("SNAPSHOT_TTL", 300);
        String secret = System.getenv("CURSOR_SECRET");
        if (secret == null || secret.isBlank()) {
            secret = CursorService.randomSecret();
        }

        HttpServerApp app = new HttpServerApp(port, Duration.ofSeconds(ttlSeconds), secret);
        int actualPort = app.start();

        Runtime.getRuntime().addShutdownHook(new Thread(app::stop, "shutdown"));

        System.out.println("游标分页服务已启动");
        System.out.println("  监听端口:       " + actualPort);
        System.out.println("  快照 TTL:       " + ttlSeconds + " 秒");
        System.out.println("  示例:  curl 'http://localhost:" + actualPort
                + "/api/items?sort=name_asc&pageSize=5'");
        // 阻塞主线程，由 shutdown hook 负责停止
        Thread.currentThread().join();
    }

    private static int envInt(String name, int def) {
        String raw = System.getenv(name);
        if (raw == null || raw.isBlank()) {
            return def;
        }
        try {
            return Integer.parseInt(raw.trim());
        } catch (NumberFormatException e) {
            System.err.println("环境变量 " + name + " 不是整数: " + raw + "，使用默认值 " + def);
            return def;
        }
    }

    private Main() {
    }
}
