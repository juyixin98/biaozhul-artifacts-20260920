package com.opp16.engine;

import com.opp16.engine.json.Json;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * JSON request entry point for the single-node in-memory query engine.
 *
 * <pre>
 *   java com.opp16.engine.Main [options] [request.json]
 *
 *   (no request file)          read request JSON from stdin
 *   --out PATH                 write full response JSON to PATH (default: stdout)
 *   --export-plan PATH         export logical plan text
 *   --export-data PATH         export table data as JSON
 *   --export-selection PATH    export the selection vector as a JSON index array
 *   --export-result PATH       export the materialised result as JSON
 *   -h, --help                 usage
 *
 * Exit codes: 0 success, 1 unexpected failure, 2 request error
 * (invalid selection subscript, bad predicate, malformed JSON, ...).
 * </pre>
 */
public final class Main {

    public static void main(String[] args) {
        String requestFile = null;
        String out = null;
        String exportPlan = null, exportData = null, exportSelection = null, exportResult = null;

        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "-h", "--help" -> { printUsage(); System.exit(0); }
                case "--out" -> out = requireValue(args, ++i, "--out");
                case "--export-plan" -> exportPlan = requireValue(args, ++i, "--export-plan");
                case "--export-data" -> exportData = requireValue(args, ++i, "--export-data");
                case "--export-selection" -> exportSelection = requireValue(args, ++i, "--export-selection");
                case "--export-result" -> exportResult = requireValue(args, ++i, "--export-result");
                default -> {
                    if (args[i].startsWith("--")) {
                        System.err.println("unknown option: " + args[i]);
                        printUsage();
                        System.exit(1);
                    }
                    if (requestFile != null) {
                        System.err.println("unexpected extra argument: " + args[i]);
                        System.exit(1);
                    }
                    requestFile = args[i];
                }
            }
        }

        try {
            String requestText = requestFile == null
                    ? new String(System.in.readAllBytes(), StandardCharsets.UTF_8)
                    : Files.readString(Path.of(requestFile), StandardCharsets.UTF_8);

            QueryEngine engine = new QueryEngine();
            QueryEngine.Response response = engine.handle(requestText);

            if (out == null) {
                System.out.println(response.render());
            } else {
                Files.writeString(Path.of(out), response.render() + "\n", StandardCharsets.UTF_8);
                System.err.println("response written to " + out + " (status=" + response.status + ")");
            }

            if (response.status == 0) {
                Json.Obj body = response.json;
                if (exportPlan != null) {
                    Files.writeString(Path.of(exportPlan), body.get("plan").asString() + "\n",
                            StandardCharsets.UTF_8);
                }
                if (exportData != null) {
                    Files.writeString(Path.of(exportData),
                            body.get("data").render(true) + "\n", StandardCharsets.UTF_8);
                }
                if (exportSelection != null) {
                    Files.writeString(Path.of(exportSelection),
                            body.get("selection").render() + "\n", StandardCharsets.UTF_8);
                }
                if (exportResult != null) {
                    Files.writeString(Path.of(exportResult),
                            body.get("result").render(true) + "\n", StandardCharsets.UTF_8);
                }
            }
            System.exit(response.status);
        } catch (java.io.IOException e) {
            System.err.println("IO error: " + e.getMessage());
            System.exit(1);
        }
    }

    private static String requireValue(String[] args, int i, String flag) {
        if (i >= args.length) {
            System.err.println(flag + " requires a path argument");
            System.exit(1);
        }
        return args[i];
    }

    private static void printUsage() {
        System.err.println("""
                Usage: java com.opp16.engine.Main [options] [request.json]
                  (no request file)      read request JSON from stdin
                  --out PATH             write full response JSON to PATH (default stdout)
                  --export-plan PATH     export logical plan text
                  --export-data PATH     export table data JSON
                  --export-selection PATH export selection-vector index array JSON
                  --export-result PATH   export materialised result JSON
                  -h, --help             show this help""");
    }
}
