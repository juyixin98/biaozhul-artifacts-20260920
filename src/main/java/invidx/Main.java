package invidx;

import invidx.corpus.CorpusGenerator;
import invidx.engine.CrashHook;
import invidx.engine.IndexConfig;
import invidx.engine.InvertedIndex;
import invidx.server.JsonHttpServer;

import java.net.InetAddress;
import java.nio.file.Path;
import java.time.Duration;
import java.util.List;

/**
 * Entry point.
 *
 * <pre>
 *   java -jar segment-merge-index.jar [--dir data] [--port 8080]
 *        [--seed 42] [--docs 200] [--buffer 8] [--merge-factor 3]
 *        [--merge-interval-ms 1000] [--no-background-merge]
 * </pre>
 *
 * On first open of an empty data directory the synthetic corpus is
 * generated and indexed, so the service answers meaningful queries
 * immediately.
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        Args parsed = Args.parse(args);

        boolean fresh = !java.nio.file.Files.exists(
                parsed.dir.resolve(invidx.catalog.ManifestStore.FILE_NAME));
        IndexConfig config = new IndexConfig(parsed.buffer, parsed.backgroundMerge,
                Duration.ofMillis(parsed.mergeIntervalMs), parsed.mergeFactor);
        CrashHook hook = parsed.crashAt == null ? CrashHook.NOOP : oneShotCrash(parsed.crashAt);
        InvertedIndex index = InvertedIndex.open(parsed.dir, config, hook);

        if (parsed.docs > 0 && fresh) {
            CorpusGenerator generator = new CorpusGenerator(parsed.seed);
            List<String> corpus = generator.generate(parsed.docs);
            for (String text : corpus) {
                index.addDocument(text);
            }
            index.flush();
            System.out.println("seeded " + corpus.size() + " synthetic documents");
        }

        JsonHttpServer http = new JsonHttpServer(index, parsed.port);
        http.start();
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            http.close();
            index.close();
        }));
        System.out.println("inverted-index service on http://"
                + InetAddress.getLoopbackAddress().getHostAddress() + ":" + http.port());
        System.out.println("data directory: " + parsed.dir.toAbsolutePath());
    }

    /**
     * Hard-kill the JVM the first time the named durability point is
     * reached — emulates {@code kill -9} / power loss without running
     * shutdown hooks (so close() cannot flush pending work).
     */
    private static CrashHook oneShotCrash(String point) {
        return new CrashHook() {
            private volatile boolean fired;

            @Override
            public void onPoint(String p) {
                if (!fired && p.equals(point)) {
                    fired = true;
                    System.err.println("CRASH-INJECTION: hard halt at " + p);
                    System.err.flush();
                    Runtime.getRuntime().halt(91);
                }
            }
        };
    }

    private record Args(Path dir, int port, int docs, long seed, int buffer,
                        int mergeFactor, long mergeIntervalMs, boolean backgroundMerge,
                        String crashAt) {

        static Args parse(String[] argv) {
            Path dir = Path.of("data");
            int port = 8080;
            int docs = 200;
            long seed = 42L;
            int buffer = 8;
            int mergeFactor = 3;
            long mergeIntervalMs = 1000;
            boolean backgroundMerge = true;
            String crashAt = null;
            for (int i = 0; i < argv.length; i++) {
                switch (argv[i]) {
                    case "--dir" -> dir = Path.of(argv[++i]);
                    case "--port" -> port = Integer.parseInt(argv[++i]);
                    case "--docs" -> docs = Integer.parseInt(argv[++i]);
                    case "--seed" -> seed = Long.parseLong(argv[++i]);
                    case "--buffer" -> buffer = Integer.parseInt(argv[++i]);
                    case "--merge-factor" -> mergeFactor = Integer.parseInt(argv[++i]);
                    case "--merge-interval-ms" -> mergeIntervalMs = Long.parseLong(argv[++i]);
                    case "--no-background-merge" -> backgroundMerge = false;
                    case "--crash-at" -> crashAt = argv[++i];
                    case "--help", "-h" -> printHelpAndExit();
                    default -> throw new IllegalArgumentException("unknown argument: " + argv[i]);
                }
            }
            return new Args(dir, port, docs, seed, buffer, mergeFactor,
                    mergeIntervalMs, backgroundMerge, crashAt);
        }

        private static void printHelpAndExit() {
            System.out.println("""
                    Usage: segment-merge-index [options]
                      --dir PATH                 index data directory (default: data)
                      --port N                   HTTP port (default: 8080)
                      --docs N                   synthetic docs to seed when empty (default: 200)
                      --seed N                   corpus RNG seed (default: 42)
                      --buffer N                 RAM buffer size before flush (default: 8)
                      --merge-factor N           segments per merge (default: 3)
                      --merge-interval-ms N      background merge period (default: 1000)
                      --no-background-merge      disable the merge timer
                      --crash-at POINT           hard-halt at flush.tmp|flush.rename|flush.manifest
                                                 |merge.tmp|merge.rename|merge.manifest""");
            System.exit(0);
        }
    }
}
