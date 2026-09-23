package seqcep;

import seqcep.engine.MatchEngine;
import seqcep.engine.WalCorruptionException;
import seqcep.http.ApiServer;

import java.nio.file.Path;

/**
 * Entry point. Configuration via environment variables (sensible defaults included):
 * <dl>
 *   <dt>SEQCEP_PORT</dt><dd>TCP port, default 8080 (0 = pick a free port)</dd>
 *   <dt>SEQCEP_DATA_DIR</dt><dd>directory for {@code events.log} WAL, default {@code ./data}</dd>
 *   <dt>SEQCEP_ENGINE_ID</dt><dd>engine identity returned in responses, default {@code default}</dd>
 * </dl>
 */
public final class Main {

    public static void main(String[] args) {
        int port = parseIntEnv("SEQCEP_PORT", 8080);
        String dataDir = System.getenv().getOrDefault("SEQCEP_DATA_DIR", "data");
        String engineId = System.getenv().getOrDefault("SEQCEP_ENGINE_ID", "default");

        MatchEngine engine;
        try {
            engine = MatchEngine.open(engineId, Path.of(dataDir, "events.log"));
        } catch (WalCorruptionException e) {
            System.err.println("[seqcep] FATAL: cannot recover write-ahead log: " + e.getMessage());
            System.err.println("[seqcep] Inspect " + Path.of(dataDir, "events.log")
                    + ", or remove it deliberately after confirming the data can be discarded.");
            System.exit(2);
            return;
        }

        ApiServer api;
        try {
            api = new ApiServer(engine, port);
        } catch (Exception e) {
            System.err.println("[seqcep] FATAL: cannot bind HTTP server: " + e.getMessage());
            System.exit(3);
            return;
        }
        api.start();
        int bound = api.port();

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            System.out.println("[seqcep] shutting down, closing WAL...");
            api.stop();
            engine.close();
        }));

        System.out.println("[seqcep] engine '" + engineId + "' listening on http://localhost:" + bound);
        System.out.println("[seqcep] WAL: " + Path.of(dataDir, "events.log").toAbsolutePath());
        System.out.println("[seqcep] pattern window: " + MatchEngine.WINDOW_MS + " ms");
    }

    private static int parseIntEnv(String name, int dflt) {
        String raw = System.getenv(name);
        if (raw == null || raw.isBlank()) return dflt;
        try {
            return Integer.parseInt(raw.trim());
        } catch (NumberFormatException e) {
            System.err.println("[seqcep] ignoring invalid " + name + "=" + raw);
            return dflt;
        }
    }

    private Main() {}
}
