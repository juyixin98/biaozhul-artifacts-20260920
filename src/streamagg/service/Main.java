package streamagg.service;

/**
 * 服务入口。
 *
 * <pre>
 * java -cp build streamagg.service.Main [port] [reconcilePeriodMillis]
 * </pre>
 *
 * 默认端口 8080；reconcilePeriodMillis 为 0（默认）表示不做周期对账，
 * 可通过 POST /reconcile 手动对账。
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) {
        int port = 8080;
        long reconcilePeriod = 0L;
        if (args.length >= 1) {
            port = Integer.parseInt(args[0]);
        }
        if (args.length >= 2) {
            reconcilePeriod = Long.parseLong(args[1]);
        }

        HttpService service = new HttpService(
                port, new streamagg.core.SystemClock(), new streamagg.core.RealScheduler(),
                reconcilePeriod);
        service.start();
        int actualPort = service.getPort();
        System.out.println("streamagg 服务已启动");
        System.out.println("  监听端口: " + actualPort);
        System.out.println("  周期对账: "
                + (reconcilePeriod > 0 ? reconcilePeriod + "ms" : "关闭（POST /reconcile 手动触发）"));
        System.out.println("  健康检查: curl http://localhost:" + actualPort + "/health");
        System.out.println("按 Ctrl+C 退出");

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            System.out.println("正在关闭服务...");
            service.close();
        }));

        try {
            Thread.currentThread().join();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
