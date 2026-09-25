package com.example.dedup;

import com.example.dedup.pipeline.PipelineConfig;
import com.example.dedup.service.HttpService;

import java.nio.file.Path;
import java.util.HashMap;
import java.util.Map;

/**
 * Command-line entry point.
 *
 * Example:
 *   java com.example.dedup.Main --port 8080 --mode manual \
 *       --window-ms 10000 --lateness-ms 5000 --retention-ms 5000 \
 *       --max-entries 100000 --snapshot-file build/state.json
 */
public final class Main {

    public static void main(String[] args) throws Exception {
        Map<String, String> a = parseArgs(args);

        PipelineConfig d = PipelineConfig.defaults();
        int port = Integer.parseInt(a.getOrDefault("port", "8080"));
        String mode = a.getOrDefault("mode", "manual");

        PipelineConfig config = new PipelineConfig(
                Integer.parseInt(a.getOrDefault("max-entries", String.valueOf(d.maxEntries()))),
                Long.parseLong(a.getOrDefault("retention-ms", String.valueOf(d.retentionMillis()))),
                Integer.parseInt(a.getOrDefault("max-tombstone-keys", String.valueOf(d.maxTombstoneKeys()))),
                Long.parseLong(a.getOrDefault("tombstone-ttl-ms", String.valueOf(d.tombstoneTtlMillis()))),
                Long.parseLong(a.getOrDefault("window-ms", String.valueOf(d.windowSizeMillis()))),
                Long.parseLong(a.getOrDefault("lateness-ms", String.valueOf(d.windowLatenessMillis()))),
                Long.parseLong(a.getOrDefault("out-of-orderness-ms", String.valueOf(d.outOfOrdernessMillis()))),
                Long.parseLong(a.getOrDefault("auto-watermark-ms", String.valueOf(d.autoWatermarkPeriodMillis()))));

        Path snapshotFile = a.containsKey("snapshot-file") ? Path.of(a.get("snapshot-file")) : null;

        HttpService service = mode.equals("wall")
                ? HttpService.wall(port, config, snapshotFile)
                : HttpService.manual(port, config, snapshotFile);
        service.start();

        System.out.println("dedup service listening on http://localhost:" + service.port()
                + " mode=" + mode
                + (snapshotFile != null ? " snapshotFile=" + snapshotFile.toAbsolutePath() : ""));

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            System.out.println("shutting down");
            service.close();
        }));

        Thread.currentThread().join();
    }

    private static Map<String, String> parseArgs(String[] args) {
        Map<String, String> m = new HashMap<>();
        for (int i = 0; i < args.length; i++) {
            String arg = args[i];
            if (!arg.startsWith("--") || i + 1 >= args.length) {
                throw new IllegalArgumentException("expected --key value, got: " + arg);
            }
            m.put(arg.substring(2), args[++i]);
        }
        return m;
    }
}
