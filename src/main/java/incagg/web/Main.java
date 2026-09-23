package incagg.web;

import com.sun.net.httpserver.HttpServer;
import incagg.store.IncrementalViewStore;

import java.net.InetSocketAddress;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadPoolExecutor;

/**
 * 服务入口。仅依赖 JDK 自带的 com.sun.net.httpserver.HttpServer。
 *
 * 启动：java -cp build/classes incagg.web.Main [port]
 * 默认端口 8080，可用参数或环境变量 PORT 覆盖。
 */
public final class Main {

    public static void main(String[] args) throws Exception {
        int port = port(args);
        IncrementalViewStore store = new IncrementalViewStore();
        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/", new ApiHandler(store));
        // HttpServer 默认是单线程分发，显式给一个线程池以支持并发；store 自身 synchronized。
        server.setExecutor(Executors.newFixedThreadPool(8));
        server.start();
        System.out.println("增量连接聚合视图服务已启动: http://localhost:" + port);
        System.out.println("接口: GET /health | POST /events | POST /events/batch |"
                + " GET /view | GET /view/recompute | GET /view/diff | POST /admin/reset");
    }

    private static int port(String[] args) {
        if (args.length > 0) return Integer.parseInt(args[0]);
        String env = System.getenv("PORT");
        if (env != null && !env.isBlank()) return Integer.parseInt(env.trim());
        return 8080;
    }
}
