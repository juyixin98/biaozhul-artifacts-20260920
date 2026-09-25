package phrase;

import phrase.core.Token;
import phrase.server.ApiServer;
import phrase.search.DocMatch;
import phrase.search.Match;
import phrase.search.SearchResult;
import phrase.search.SearchService;

import java.util.List;

/**
 * 命令行入口。
 *
 * <pre>
 *   java phrase.Main serve [--port 8080] [--field-gap 0] [--max-matches 100]
 *   java phrase.Main search "echo echo" [--slop 0] [--analyzer standard] [--field body]
 *                          [--terms a,b,c] [--field-gap 0]
 * </pre>
 */
public final class Main {

    private Main() {}

    public static void main(String[] args) throws Exception {
        if (args.length == 0) {
            usage();
            System.exit(2);
        }
        switch (args[0]) {
            case "serve" -> serve(args);
            case "search" -> search(args);
            case "-h", "--help", "help" -> {
                usage();
            }
            default -> {
                System.err.println("unknown command: " + args[0]);
                usage();
                System.exit(2);
            }
        }
    }

    private static void serve(String[] args) throws Exception {
        int port = 8080;
        int fieldGap = 0;
        int maxMatches = 100;
        for (int i = 1; i < args.length; i++) {
            switch (args[i]) {
                case "--port" -> port = parseIntArg(args, ++i, "port");
                case "--field-gap" -> fieldGap = parseIntArg(args, ++i, "field-gap");
                case "--max-matches" -> maxMatches = parseIntArg(args, ++i, "max-matches");
                default -> throw new IllegalArgumentException("unknown option: " + args[i]);
            }
        }
        SearchService service = new SearchService(fieldGap, maxMatches);
        try (ApiServer api = new ApiServer(service, port)) {
            api.start();
            System.out.println("phrase-search service listening on http://127.0.0.1:" + api.port());
            System.out.println("endpoints: POST /search, POST /analyze, GET /corpus, GET /health");
            // 阻塞直到被中断
            Thread.currentThread().join();
        }
    }

    private static void search(String[] args) {
        if (args.length < 2) {
            throw new IllegalArgumentException("search requires a query string");
        }
        String query = args[1];
        int slop = 0;
        String analyzer = "standard";
        String field = null;
        List<String> terms = null;
        int fieldGap = 0;
        int maxMatches = 100;
        for (int i = 2; i < args.length; i++) {
            switch (args[i]) {
                case "--slop" -> slop = parseIntArg(args, ++i, "slop");
                case "--analyzer" -> analyzer = args[++i];
                case "--field" -> field = args[++i];
                case "--terms" -> terms = List.of(args[++i].split(","));
                case "--field-gap" -> fieldGap = parseIntArg(args, ++i, "field-gap");
                case "--max-matches" -> maxMatches = parseIntArg(args, ++i, "max-matches");
                default -> throw new IllegalArgumentException("unknown option: " + args[i]);
            }
        }
        SearchService service = new SearchService(fieldGap, maxMatches);
        SearchResult r = service.search(analyzer, query, terms, slop, field);
        printResult(r);
    }

    private static void printResult(SearchResult r) {
        System.out.println("analyzer   : " + r.analyzer());
        System.out.println("slop       : " + r.slop());
        System.out.println("field      : " + (r.field() == null ? "(all fields)" : r.field()));
        System.out.println("queryTerms : " + r.queryTerms());
        System.out.println("totalHits  : " + r.totalHits() + (r.truncated() ? " (truncated)" : ""));
        for (DocMatch dm : r.hits()) {
            System.out.println("  doc " + dm.docId() + "  matches=" + dm.matchCount()
                    + (dm.truncated() ? " (truncated)" : ""));
            for (Match m : dm.matches()) {
                System.out.println("    positions=" + m.positions()
                        + " fields=" + m.fields()
                        + (m.crossField() ? "  [cross-field]" : ""));
            }
        }
        if (r.hits().isEmpty()) {
            System.out.println("  (no documents matched)");
        }
    }

    private static int parseIntArg(String[] args, int i, String name) {
        if (i >= args.length) {
            throw new IllegalArgumentException("missing value for --" + name);
        }
        try {
            return Integer.parseInt(args[i]);
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException("--" + name + " must be an integer");
        }
    }

    private static void usage() {
        System.out.println("""
                Usage:
                  java phrase.Main serve [--port 8080] [--field-gap 0] [--max-matches 100]
                  java phrase.Main search "query text" [--slop 0] [--analyzer standard|stopword]
                                          [--field FIELD] [--terms t1,t2,...]
                                          [--field-gap 0] [--max-matches 100]
                  java phrase.Main help

                slop: maximum number of other tokens allowed between adjacent phrase terms
                      (0 = exact adjacent phrase).
                analyzer:
                  standard - tokenize + lowercase, no stopword removal
                  stopword - remove built-in stopwords but KEEP their original positions
                """);
    }
}
