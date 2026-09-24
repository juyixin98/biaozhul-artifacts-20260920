package com.example.diff;

import com.example.diff.server.HttpServerMain;

/**
 * Command line entry point.
 *
 * <pre>
 *   diff file-old file-new [maxEditDistance] [maxInputChars] [context]
 *       prints JSON diff to stdout
 *   diff --udiff file-old file-new [maxEditDistance] [maxInputChars] [context]
 *       prints unified-diff text
 *   server [port]
 *       starts the JSON HTTP service (default 8080)
 * </pre>
 */
public final class Main {

    public static void main(String[] args) {
        if (args.length == 0) {
            usage();
            System.exit(2);
        }
        switch (args[0]) {
            case "server": {
                int port = args.length > 1 ? Integer.parseInt(args[1]) : 8080;
                HttpServerMain.start(port);
                break;
            }
            case "diff":
            case "--udiff": {
                if (args.length < 3) {
                    usage();
                    System.exit(2);
                }
                boolean unified = args[0].equals("--udiff");
                int idx = 1;
                String oldText = readFile(args[idx++]);
                String newText = readFile(args[idx++]);
                int maxD = args.length > idx ? Integer.parseInt(args[idx++]) : MyersDiff.UNLIMITED_DISTANCE;
                int maxChars = args.length > idx ? Integer.parseInt(args[idx++]) : MyersDiff.DEFAULT_MAX_INPUT_CHARS;
                int context = args.length > idx ? Integer.parseInt(args[idx]) : 3;
                MyersDiff diff = new MyersDiff(maxD, maxChars, context);
                DiffResult result = diff.diff(oldText, newText);
                if (unified) {
                    System.out.print(UnifiedDiff.render(result));
                } else {
                    System.out.println(com.example.diff.json.Json.writePretty(com.example.diff.server.Dto.diffResult(result)));
                }
                break;
            }
            default:
                usage();
                System.exit(2);
        }
    }

    private static String readFile(String path) {
        try {
            return java.nio.file.Files.readString(java.nio.file.Paths.get(path));
        } catch (Exception e) {
            throw new RuntimeException("cannot read " + path + ": " + e.getMessage(), e);
        }
    }

    private static void usage() {
        System.err.println("usage:");
        System.err.println("  Main diff <oldFile> <newFile> [maxEditDistance] [maxInputChars] [context]");
        System.err.println("  Main --udiff <oldFile> <newFile> [maxEditDistance] [maxInputChars] [context]");
        System.err.println("  Main server [port]");
    }

    private Main() {
    }
}
