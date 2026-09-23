package com.example.uninorm;

/**
 * Entry point: builds the synthetic corpus, starts the JSON service.
 *
 * <pre>
 *   java -cp build/classes com.example.uninorm.Main [port]
 * </pre>
 *
 * Default port: 8080. Binds to 127.0.0.1 only.
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        int port = args.length > 0 ? Integer.parseInt(args[0]) : 8080;

        SearchEngine engine = new SearchEngine();
        for (Document d : Corpus.documents()) {
            engine.addDocument(d);
        }

        SearchServer server = new SearchServer(engine);
        server.start(port);
        System.out.println("unicode-norm-map service listening on http://127.0.0.1:"
                + server.port());
        System.out.println("loaded " + engine.documentCount()
                + " synthetic documents");
        System.out.println("try: curl 'http://127.0.0.1:" + server.port()
                + "/search?q=strasse'");
    }
}
