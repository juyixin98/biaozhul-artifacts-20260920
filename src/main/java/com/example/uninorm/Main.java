package com.example.uninorm;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

/**
 * Entry point.
 *
 *   java ... Main server [port] [corpusFile]   start the JSON service
 *   java ... Main normalize "text"             print normalization + mapping as JSON
 *   java ... Main search "query" [corpusFile]  load corpus, search, print hits as JSON
 *
 * Corpus file format: documents separated by header lines "### <docId>",
 * everything after a header until the next header is the document text.
 */
public final class Main {

    public static void main(String[] args) throws Exception {
        String cmd = args.length > 0 ? args[0] : "server";
        switch (cmd) {
            case "server": {
                int port = args.length > 1 ? Integer.parseInt(args[1]) : 8080;
                Server server = new Server();
                if (args.length > 2) {
                    loadCorpus(server.engine(), Path.of(args[2]));
                }
                int actual = server.start(port);
                System.out.println("listening on http://127.0.0.1:" + actual
                        + " (" + server.engine().documentCount() + " documents)");
                break;
            }
            case "normalize": {
                String text = args.length > 1 ? args[1] : "";
                NormalizedText nt = TextNormalizer.normalize(text);
                System.out.println(Json.encode(Json.obj(
                        "original", text,
                        "normalized", nt.text)));
                break;
            }
            case "search": {
                String query = args.length > 1 ? args[1] : "";
                SearchEngine engine = new SearchEngine();
                if (args.length > 2) {
                    loadCorpus(engine, Path.of(args[2]));
                }
                List<SearchEngine.Hit> hits = engine.search(query, 100);
                java.util.List<Object> out = new java.util.ArrayList<>();
                for (SearchEngine.Hit h : hits) {
                    out.add(Json.obj("docId", h.docId, "start", h.start,
                            "end", h.end, "matched", h.matched));
                }
                System.out.println(Json.encode(Json.obj("hits", out)));
                break;
            }
            default:
                System.err.println("unknown command: " + cmd);
                System.exit(2);
        }
    }

    static void loadCorpus(SearchEngine engine, Path file) throws IOException {
        String content = Files.readString(file, StandardCharsets.UTF_8);
        String id = null;
        StringBuilder buf = new StringBuilder();
        for (String line : content.split("\n", -1)) {
            if (line.startsWith("### ")) {
                if (id != null) engine.addDocument(id, buf.toString().strip());
                id = line.substring(4).strip();
                buf.setLength(0);
            } else {
                buf.append(line).append('\n');
            }
        }
        if (id != null) engine.addDocument(id, buf.toString().strip());
    }
}
