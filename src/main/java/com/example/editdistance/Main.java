package com.example.editdistance;

import java.util.List;

/**
 * Entry point: builds the synthetic corpus, creates the search service, and starts
 * the JSON HTTP server.
 *
 * <p>Usage: {@code java -jar edit-distance-filter-1.0.0.jar [--port 8080]
 * [--corpus-size 2000] [--seed 42] [--normalize NFC]}
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        int port = intArg(args, "--port", 8080);
        int corpusSize = intArg(args, "--corpus-size", 2000);
        long seed = longArg(args, "--seed", 42L);
        Normalize normalize = Normalize.valueOf(stringArg(args, "--normalize", "NFC"));

        List<String> corpus = CorpusGenerator.generate(corpusSize, seed);
        SearchService service = new SearchService(normalize);
        service.rebuild(corpus);

        HttpApi api = new HttpApi(service, corpusSize, seed);
        api.start(port);

        System.out.printf("edit-distance-filter listening on http://localhost:%d/%n", api.port());
        System.out.printf("corpus=%d terms, normalize=%s%n", service.corpusSize(), normalize);
        System.out.println("endpoints: GET /health, GET /corpus, POST /corpus/reload, POST /search");
    }

    private static int intArg(String[] args, String name, int defaultValue) {
        return (int) longArg(args, name, defaultValue);
    }

    private static long longArg(String[] args, String name, long defaultValue) {
        for (int i = 0; i + 1 < args.length; i++) {
            if (args[i].equals(name)) {
                return Long.parseLong(args[i + 1]);
            }
        }
        return defaultValue;
    }

    private static String stringArg(String[] args, String name, String defaultValue) {
        for (int i = 0; i + 1 < args.length; i++) {
            if (args[i].equals(name)) {
                return args[i + 1];
            }
        }
        return defaultValue;
    }
}
