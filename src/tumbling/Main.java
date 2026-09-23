package tumbling;

/**
 * 服务入口。
 *
 * 用法:
 *   java tumbling.Main [port] [windowSize] [allowedLateness]
 * 也可用环境变量: PORT / WINDOW_SIZE / ALLOWED_LATENESS（命令行参数优先）。
 */
public final class Main {

    public static void main(String[] args) throws Exception {
        int port = (int) resolve(args, 0, "PORT", 8080);
        long windowSize = resolve(args, 1, "WINDOW_SIZE", 10L);
        long allowedLateness = resolve(args, 2, "ALLOWED_LATENESS", 2L);

        Server server = new Server(port, windowSize, allowedLateness);
        server.start();
        int boundPort = server.getAddressPort();
        System.out.println("事件时间滚动窗口计数服务已启动");
        System.out.println("  监听端口        : " + boundPort);
        System.out.println("  窗口大小        : " + windowSize);
        System.out.println("  迟到容忍期      : " + allowedLateness);
        System.out.println("  健康检查        : GET  http://localhost:" + boundPort + "/health");
        System.out.println("  全量状态        : GET  http://localhost:" + boundPort + "/snapshot");
        System.out.println("按 Ctrl+C 停止。");

        Runtime.getRuntime().addShutdownHook(new Thread(() -> server.close(), "shutdown"));
        Thread.currentThread().join();
    }

    private static long resolve(String[] args, int idx, String env, long dflt) {
        if (args.length > idx && !args[idx].isBlank()) return Long.parseLong(args[idx].trim());
        String v = System.getenv(env);
        if (v != null && !v.isBlank()) return Long.parseLong(v.trim());
        return dflt;
    }
}
