package com.example.vercov;

import com.example.vercov.api.HttpApi;
import com.example.vercov.store.VersionStore;

/**
 * Entry point. Starts the HTTP API on {@code PORT} (default 8080).
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(System.getenv().getOrDefault("PORT", "8080"));
        HttpApi api = new HttpApi(new VersionStore(), port);
        api.start();
        System.out.println("version-interval-coverage listening on port " + api.port());
        Thread.currentThread().join();
    }
}
