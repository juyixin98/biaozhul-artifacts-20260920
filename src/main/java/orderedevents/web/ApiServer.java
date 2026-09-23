package orderedevents.web;

import java.net.InetSocketAddress;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.atomic.AtomicInteger;

import com.sun.net.httpserver.HttpServer;

import orderedevents.service.EventService;
import orderedevents.service.ServerConfig;

/** Wraps the JDK {@link HttpServer}: bounded request thread pool, single router, graceful stop. */
public final class ApiServer {

    private final ServerConfig config;
    private final EventService service;
    private HttpServer server;
    private ExecutorService httpPool;

    public ApiServer(ServerConfig config, EventService service) {
        this.config = config;
        this.service = service;
    }

    public void start() {
        try {
            server = HttpServer.create(new InetSocketAddress(config.port()), 0);
        } catch (Exception e) {
            throw new IllegalStateException("failed to bind HTTP server on port " + config.port(), e);
        }
        AtomicInteger n = new AtomicInteger();
        ThreadFactory tf = r -> {
            Thread t = new Thread(r, "http-" + n.incrementAndGet());
            t.setDaemon(true);
            return t;
        };
        httpPool = Executors.newFixedThreadPool(32, tf);
        server.createContext("/", new ApiHandler(service, config));
        server.setExecutor(httpPool);
        server.start();
    }

    public int boundPort() {
        return server == null ? -1 : server.getAddress().getPort();
    }

    public void stop() {
        if (server != null) {
            server.stop(0);
        }
        if (httpPool != null) {
            httpPool.shutdownNow();
        }
    }
}
