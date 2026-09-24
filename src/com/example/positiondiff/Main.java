package com.example.positiondiff;

import com.example.positiondiff.diff.Budget;
import com.example.positiondiff.diff.DiffEngine;
import com.example.positiondiff.diff.DiffResult;
import com.example.positiondiff.diff.EditOp;
import com.example.positiondiff.diff.Hunks;
import com.example.positiondiff.json.Json;
import com.example.positiondiff.json.JsonParser;
import com.example.positiondiff.model.Line;
import com.example.positiondiff.search.SearchIndex;
import com.example.positiondiff.server.Api;
import com.example.positiondiff.server.Server;
import com.example.positiondiff.text.LineSplitter;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;

/**
 * Entry point.
 *
 * <pre>
 *   serve [port]                          run the JSON HTTP service
 *   diff --file A B [--max-nodes N]       print a diff as JSON
 *   diff -                                read {oldText,newText,...} JSON from stdin
 *   apply                                 read {oldText,ops} JSON from stdin, print newText
 *   search QUERY [limit]                  search the bundled synthetic corpus
 * </pre>
 */
public final class Main {

    public static void main(String[] args) throws Exception {
        if (args.length == 0) {
            usage();
            System.exit(2);
        }
        switch (args[0]) {
            case "serve" -> serve(args);
            case "diff" -> diffCmd(args);
            case "apply" -> applyCmd();
            case "search" -> searchCmd(args);
            default -> {
                System.err.println("unknown command: " + args[0]);
                usage();
                System.exit(2);
            }
        }
    }

    private static void serve(String[] args) throws Exception {
        int port = args.length >= 2 ? Integer.parseInt(args[1]) : 8080;
        Server server = new Server();
        server.start(port);
        System.out.println("position-diff listening on http://127.0.0.1:" + server.port());
        Thread.currentThread().join();
    }

    private static void diffCmd(String[] args) throws Exception {
        long maxNodes = Budget.UNLIMITED;
        String mode = null;
        String fileA = null;
        String fileB = null;
        for (int i = 1; i < args.length; i++) {
            switch (args[i]) {
                case "--max-nodes" -> maxNodes = Long.parseLong(args[++i]);
                case "--file" -> {
                    mode = "file";
                    fileA = args[++i];
                    fileB = args[++i];
                }
                case "-" -> mode = "stdin";
                default -> throw new IllegalArgumentException("unexpected argument: " + args[i]);
            }
        }

        String oldText;
        String newText;
        Budget budget;
        int context = 3;
        if ("file".equals(mode)) {
            oldText = Files.readString(Path.of(fileA), StandardCharsets.UTF_8);
            newText = Files.readString(Path.of(fileB), StandardCharsets.UTF_8);
            budget = new Budget(maxNodes, Budget.UNLIMITED);
        } else {
            Map<String, Object> req = JsonParser.parseObject(readStdin());
            oldText = Api.decodeText(req, "oldText");
            newText = Api.decodeText(req, "newText");
            budget = Api.decodeBudget(req);
            context = (int) Json.getLong(req, "context", 3);
        }

        var oldLines = LineSplitter.split(oldText);
        var newLines = LineSplitter.split(newText);
        DiffResult result = DiffEngine.diff(oldLines, newLines, budget);
        var out = Api.encodeResult(result, oldLines, newLines, Math.max(0, context));
        out.put("unifiedDiff", Hunks.toUnifiedDiff(
                fileA == null ? "old" : fileA,
                fileB == null ? "new" : fileB,
                result.hunks()));
        System.out.print(Json.stringify(out));
        if (!result.optimal()) {
            System.err.println("NOTE: degraded result - script is valid but NOT proven shortest");
        }
    }

    private static void applyCmd() throws Exception {
        Map<String, Object> req = JsonParser.parseObject(readStdin());
        String oldText = Api.decodeText(req, "oldText");
        var opsInput = Json.getArray(req, "ops");
        if (opsInput == null) throw new IllegalArgumentException("missing 'ops' array");
        var oldLines = LineSplitter.split(oldText);
        var ops = Api.decodeOps(opsInput, oldLines);
        var newLines = DiffEngine.apply(oldLines, ops);
        var out = Json.obj();
        out.put("newText", LineSplitter.join(newLines));
        out.put("newTextLines", linesAsJson(newLines));
        System.out.print(Json.stringify(out));
    }

    private static void searchCmd(String[] args) {
        if (args.length < 2) {
            System.err.println("usage: search QUERY [limit]");
            System.exit(2);
        }
        String query = args[1];
        int limit = args.length >= 3 ? Integer.parseInt(args[2]) : 10;
        SearchIndex index = SearchIndex.synthetic();
        System.out.print(Json.stringify(Api.encodeSearch(index.search(query, limit))));
    }

    private static java.util.List<Object> linesAsJson(java.util.List<Line> lines) {
        java.util.List<Object> arr = Json.arr();
        for (Line line : lines) arr.add(Api.encodeLine(line));
        return arr;
    }

    private static String readStdin() throws Exception {
        return new String(System.in.readAllBytes(), StandardCharsets.UTF_8);
    }

    private static void usage() {
        System.err.println("""
                usage:
                  java -jar position-diff.jar serve [port]
                  java -jar position-diff.jar diff --file A B [--max-nodes N]
                  java -jar position-diff.jar diff -        (JSON {oldText,newText,budget?,context?} on stdin)
                  java -jar position-diff.jar apply         (JSON {oldText,ops} on stdin)
                  java -jar position-diff.jar search QUERY [limit]
                """);
    }
}
