package com.tjoin.service;

/**
 * 服务启动入口。
 *
 * <pre>
 *   java com.tjoin.service.Main [port]
 * </pre>
 *
 * 默认端口 8080；端口传 0 则由系统分配，启动后打印实际端口。
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        int port = 8080;
        if (args.length > 0) {
            port = Integer.parseInt(args[0]);
        }
        JoinHttpServer http = new JoinHttpServer(port);
        http.start();
        System.out.println("tjoin interval-join service listening on http://localhost:"
                + http.getPort());
        System.out.println("Endpoints: GET /health, POST /join (Ctrl+C to stop)");

        Runtime.getRuntime().addShutdownHook(new Thread(http::close, "tjoin-shutdown"));
        Thread.currentThread().join();
    }
}
