package com.example.iview.server;

import com.example.iview.view.MaterializedView;
import com.sun.net.httpserver.HttpServer;

import java.net.InetSocketAddress;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicReference;

/**
 * HTTP entry point. Pure JDK ({@code com.sun.net.httpserver.HttpServer}),
 * no third-party dependencies.
 *
 * <p>Endpoints:
 * <ul>
 *   <li>POST /events         - apply one business event</li>
 *   <li>POST /events/batch   - apply an ordered array of events</li>
 *   <li>GET  /view           - current incremental aggregates + counters</li>
 *   <li>GET  /verify         - side-by-side comparison vs full recompute</li>
 *   <li>GET  /tables         - raw product / order-line rows</li>
 *   <li>POST /reset          - clear all state (in-memory)</li>
 * </ul>
 */
public final class Main {

    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(System.getenv().getOrDefault("PORT", "8080"));
        String host = System.getenv().getOrDefault("HOST", "0.0.0.0");
        int backlog = Integer.parseInt(System.getenv().getOrDefault("BACKLOG", "0"));

        AtomicReference<MaterializedView> state =
                new AtomicReference<>(new MaterializedView());

        HttpServer server = HttpServer.create(new InetSocketAddress(host, port), backlog);
        server.createContext("/", new ViewHandler(state));
        server.setExecutor(Executors.newFixedThreadPool(8));
        server.start();
        System.out.println("incremental-view listening on http://" + host + ":" + port);
    }
}
