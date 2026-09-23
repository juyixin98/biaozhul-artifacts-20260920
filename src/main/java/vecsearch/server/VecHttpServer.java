package vecsearch.server;

import vecsearch.core.Metric;
import vecsearch.core.VectorStore;

import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadPoolExecutor;

/** 基于 JDK 内置 com.sun.net.httpserver 的轻量 HTTP 服务，无第三方依赖。 */
public final class VecHttpServer {

    private final HttpServer server;
    private final VectorStore store;

    public VecHttpServer(String host, int port, Metric metric, int workerThreads) throws IOException {
        this.store = new VectorStore(metric);
        this.server = HttpServer.create(new InetSocketAddress(host, port), 0);
        this.server.createContext("/", new ApiHandler(store));
        ThreadPoolExecutor pool = (ThreadPoolExecutor) Executors.newFixedThreadPool(
                Math.max(2, workerThreads));
        this.server.setExecutor(pool);
    }

    public VectorStore store() {
        return store;
    }

    public int port() {
        return server.getAddress().getPort();
    }

    public void start() {
        server.start();
    }

    public void stop() {
        server.stop(1);
    }
}
