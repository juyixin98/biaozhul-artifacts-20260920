package ij;

/**
 * HTTP 服务入口。
 * 用法：java ij.Main [port]
 * 环境变量：PORT（端口，默认 8080）、BIND（绑定地址，默认 127.0.0.1，对外暴露用 0.0.0.0）。
 */
public final class Main {

    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(
                envOr("PORT", args.length > 0 ? args[0] : "8080"));
        String host = envOr("BIND", "127.0.0.1");

        ApiServer api = new ApiServer(new SessionRegistry(), host, port);
        api.start();

        System.out.println("dual-stream interval join service started");
        System.out.println("listen: http://" + host + ":" + api.port());
        System.out.println("health: GET /health");

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            System.out.println("shutting down...");
            api.stop();
        }));

        // 阻塞主线程；shutdown hook 停止 server 后 JVM 随最后一个非守护线程结束而退出。
        Thread.currentThread().join();
    }

    private static String envOr(String name, String dflt) {
        String v = System.getenv(name);
        return (v == null || v.isEmpty()) ? dflt : v;
    }

    private Main() {
    }
}
