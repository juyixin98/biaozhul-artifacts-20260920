package com.example.neardup;

import com.example.neardup.api.HttpService;

/** Entry point: starts the JSON service. Port from arg[0] or env PORT, default 8080. */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        int port = 8080;
        if (args.length > 0) {
            port = Integer.parseInt(args[0]);
        } else if (System.getenv("PORT") != null) {
            port = Integer.parseInt(System.getenv("PORT"));
        }
        HttpService service = new HttpService(port);
        service.start();
        System.out.println("near-dup clustering service listening on port " + service.port());
        Thread.currentThread().join();
    }
}
