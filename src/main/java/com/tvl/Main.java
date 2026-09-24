package com.tvl;

import com.tvl.http.QueryHttpServer;

/**
 * 启动入口。端口取环境变量 TVL_PORT（默认 8080）。
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(System.getenv().getOrDefault("TVL_PORT", "8080"));
        QueryHttpServer server = new QueryHttpServer(port);
        server.start();
        System.out.println("TVL query executor listening on http://0.0.0.0:"
                + server.getPort());
        System.out.println("  GET  /health");
        System.out.println("  POST /query");
    }
}
