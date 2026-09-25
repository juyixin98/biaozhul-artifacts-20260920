package neardup;

import neardup.corpus.SyntheticCorpus;
import neardup.server.ApiService;
import neardup.server.HttpApiServer;
import neardup.server.Json;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Entry point.
 *
 *   java -cp out/classes neardup.Main serve [port]
 *       Start the local JSON HTTP service (default port 8080).
 *
 *   java -cp out/classes neardup.Main run [threshold]
 *       Run the full pipeline over the built-in synthetic corpus once and
 *       print the JSON report to stdout (no server needed).
 */
public final class Main {

    public static void main(String[] args) throws Exception {
        String mode = args.length >= 1 ? args[0] : "serve";

        switch (mode) {
            case "serve" -> {
                int port = args.length >= 2 ? Integer.parseInt(args[1]) : 8080;
                HttpApiServer api = new HttpApiServer(port);
                api.start();
                int actual = api.boundPort();
                System.err.println("near-duplicate clustering service listening on http://127.0.0.1:" + actual);
                System.err.println("  GET  /health");
                System.err.println("  GET  /corpus");
                System.err.println("  POST /cluster   body: {} | {\"threshold\":0.6} | {\"texts\":[...]}");
                System.err.println("Press Ctrl+C to stop.");
                Thread.currentThread().join();
            }
            case "run" -> {
                double threshold = args.length >= 2 ? Double.parseDouble(args[1]) : 0.6;
                Map<String, Object> report = new LinkedHashMap<>();
                report.put("mode", "cli-run");
                report.put("corpusSize", SyntheticCorpus.corpus().size());
                report.put("result", ApiService.clusterResult(SyntheticCorpus.texts(), threshold));
                System.out.println(Json.stringify(report));
            }
            default -> {
                System.err.println("unknown mode: " + mode);
                System.err.println("usage: neardup.Main serve [port] | run [threshold]");
                System.exit(2);
            }
        }
    }
}
