package vecsearch;

import vecsearch.core.Metric;
import vecsearch.server.VecHttpServer;
import vecsearch.util.ApiException;

/**
 * 服务入口。
 *
 * <pre>
 * java -cp build/classes vecsearch.Main [--port 8080] [--host 0.0.0.0] [--metric L2|COSINE] [--threads 8]
 * </pre>
 */
public final class Main {

    public static void main(String[] args) throws Exception {
        String host = "0.0.0.0";
        int port = 8080;
        Metric metric = Metric.L2;
        int threads = Math.max(2, Runtime.getRuntime().availableProcessors());

        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--port", "-p" -> port = Integer.parseInt(requireValue(args, ++i, "--port"));
                case "--host", "-h" -> host = requireValue(args, ++i, "--host");
                case "--metric", "-m" -> {
                    String v = requireValue(args, ++i, "--metric");
                    Metric parsed = Metric.fromString(v);
                    if (parsed == null) {
                        throw ApiException.badRequest("unknown metric: " + v + " (expected L2 or COSINE)");
                    }
                    metric = parsed;
                }
                case "--threads", "-t" -> threads = Integer.parseInt(requireValue(args, ++i, "--threads"));
                case "--help" -> {
                    printHelp();
                    return;
                }
                default -> throw ApiException.badRequest("unknown argument: " + args[i]);
            }
        }

        VecHttpServer http = new VecHttpServer(host, port, metric, threads);
        http.start();

        String banner = """
                ============================================================
                 vector-nearest-neighbor service started
                   metric : %s
                   listen : http://%s:%d
                   threads: %d
                   try    : curl http://localhost:%d/
                ============================================================"""
                .formatted(metric, host, http.port(), threads, http.port());
        System.out.println(banner);

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            System.out.println("shutting down...");
            http.stop();
        }));
    }

    private static String requireValue(String[] args, int idx, String flag) {
        if (idx >= args.length) {
            throw ApiException.badRequest("missing value for " + flag);
        }
        return args[idx];
    }

    private static void printHelp() {
        System.out.println("""
                Usage: java -cp build/classes vecsearch.Main [options]
                  --port,-p    <int>   HTTP port (default 8080)
                  --host,-h    <str>   bind host (default 0.0.0.0)
                  --metric,-m  <str>   L2 or COSINE (default L2)
                  --threads,-t <int>   worker threads (default #cpu)
                  --help               print this help""");
    }
}
