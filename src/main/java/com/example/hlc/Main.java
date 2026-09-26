package com.example.hlc;

import com.example.hlc.clock.SystemClock;
import com.example.hlc.core.HybridLogicalClock;
import com.example.hlc.core.OverflowPolicy;
import com.example.hlc.persist.FileHlcStateStore;
import com.example.hlc.persist.HlcStateStore;
import com.example.hlc.server.HlcServer;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.nio.file.Path;
import java.util.HashMap;
import java.util.Map;

/**
 * Entry point. Usage:
 *
 * <pre>
 * java -jar hlc-backend.jar [--port 8080] [--node node-1] \
 *      [--state-file data/hlc-state.json] [--max-logical 4095] \
 *      [--overflow BUMP_PHYSICAL|THROW] [--max-drift-ms 0]
 * </pre>
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) {
        Map<String, String> opts = parseArgs(args);
        int port = Integer.parseInt(opts.getOrDefault("port", "8080"));
        String nodeId = opts.getOrDefault("node", "node-1");
        Path stateFile = Path.of(opts.getOrDefault("state-file", "data/hlc-state.json"));
        int maxLogical = Integer.parseInt(opts.getOrDefault("max-logical",
                String.valueOf(HybridLogicalClock.DEFAULT_MAX_LOGICAL)));
        OverflowPolicy overflow = OverflowPolicy.valueOf(
                opts.getOrDefault("overflow", OverflowPolicy.BUMP_PHYSICAL.name()));
        long maxDriftMs = Long.parseLong(opts.getOrDefault("max-drift-ms", "0"));

        ObjectMapper mapper = new ObjectMapper();
        HlcStateStore store = new FileHlcStateStore(stateFile, mapper);
        var restored = store.load();

        HybridLogicalClock hlc = HybridLogicalClock.builder(new SystemClock(), nodeId)
                .maxLogical(maxLogical)
                .overflowPolicy(overflow)
                .maxDriftMillis(maxDriftMs)
                .restored(restored)
                .build();

        HlcServer server = new HlcServer(hlc, store, mapper, port);
        server.start();
        Runtime.getRuntime().addShutdownHook(new Thread(server::stop));

        System.out.println("HLC backend listening on http://localhost:" + server.port()
                + " node=" + nodeId
                + " stateFile=" + stateFile.toAbsolutePath()
                + (restored.isPresent() ? " restored=" + restored.get() : " (no prior state)"));
    }

    private static Map<String, String> parseArgs(String[] args) {
        Map<String, String> opts = new HashMap<>();
        for (int i = 0; i < args.length; i++) {
            String key = args[i];
            if (!key.startsWith("--")) {
                throw new IllegalArgumentException("unexpected argument: " + key);
            }
            if (i + 1 >= args.length) {
                throw new IllegalArgumentException("missing value for " + key);
            }
            opts.put(key.substring(2), args[++i]);
        }
        return opts;
    }
}
