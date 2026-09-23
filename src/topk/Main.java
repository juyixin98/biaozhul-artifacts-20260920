package topk;

/**
 * 启动入口。
 * 用法: java -cp classes topk.Main [port] [windowMs]
 * 也可用环境变量 PORT / WINDOW_MS 覆盖默认值。
 */
public final class Main {
    public static void main(String[] args) throws Exception {
        int port = intConfig(args, 0, "PORT", 8080);
        long windowMs = longConfig(args, 1, "WINDOW_MS", 60_000L);

        TopKService service = new TopKService(windowMs);
        HttpApi api = new HttpApi(service, port);
        api.start();

        Runtime.getRuntime().addShutdownHook(new Thread(api::stop));
        System.out.printf("topk-service listening on port %d, windowMs=%d%n", api.port(), windowMs);
    }

    private static int intConfig(String[] args, int idx, String env, int dflt) {
        if (args.length > idx) return Integer.parseInt(args[idx]);
        String v = System.getenv(env);
        return v != null ? Integer.parseInt(v) : dflt;
    }

    private static long longConfig(String[] args, int idx, String env, long dflt) {
        if (args.length > idx) return Long.parseLong(args[idx]);
        String v = System.getenv(env);
        return v != null ? Long.parseLong(v) : dflt;
    }
}
