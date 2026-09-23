package com.example.stablepager;

/**
 * Application entry point.
 *
 * <p>Configuration (all optional, environment variables):
 * <ul>
 *   <li>{@code PORT} — TCP port, default {@code 8080};</li>
 *   <li>{@code PAGER_SNAPSHOT_TTL_MS} — snapshot lifetime, default {@code 60000};</li>
 *   <li>{@code PAGER_MAC_SECRET} — HMAC key for cursor signing. Intentionally has
 *       no production default: if unset, a per-process random secret is generated
 *       (cursors stop surviving restarts) and a warning is printed.</li>
 * </ul>
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(env("PORT", "8080"));
        long ttl = Long.parseLong(env("PAGER_SNAPSHOT_TTL_MS", String.valueOf(WebRuntime.defaultTtlMillis())));
        String secret = System.getenv("PAGER_MAC_SECRET");
        boolean ephemeralSecret = false;
        if (secret == null || secret.isBlank()) {
            secret = java.util.UUID.randomUUID().toString();
            ephemeralSecret = true;
        }

        WebRuntime runtime = WebRuntime.start(port, ttl, secret);
        SeedData.seed(runtime.store());

        System.out.println("stable-pager listening on http://localhost:" + runtime.port());
        System.out.println("snapshot TTL: " + ttl + " ms");
        if (ephemeralSecret) {
            System.out.println("WARNING: PAGER_MAC_SECRET is not set; using a random per-process secret, "
                    + "cursors will not survive a restart.");
        }
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            runtime.close();
            System.out.println("stable-pager stopped");
        }));
    }

    private static String env(String name, String fallback) {
        String v = System.getenv(name);
        return v == null || v.isBlank() ? fallback : v;
    }
}
