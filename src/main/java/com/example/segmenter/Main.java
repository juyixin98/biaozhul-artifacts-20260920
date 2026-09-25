package com.example.segmenter;

import com.example.segmenter.data.CorpusLoader;
import com.example.segmenter.model.Dictionary;
import com.example.segmenter.service.SegmentServer;

import java.io.IOException;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 服务入口。
 *
 * <pre>
 * java -cp build/classes com.example.segmenter.Main \
 *      [--port 8080] [--data-dir data/corpora] [--unknown-cost 10.0] [--default dict_v1]
 * </pre>
 *
 * 默认加载 data/corpora 下所有 *.corpus 文件（文件名去扩展名为版本名，按名字排序），
 * 第一个为默认版本（除非用 --default 指定）。
 */
public final class Main {

    private int port = 8080;
    private Path dataDir = Paths.get("data", "corpora");
    private double unknownCost = com.example.segmenter.api.Segmenter.DEFAULT_UNKNOWN_CHAR_COST;
    private String defaultVersion;

    public static void main(String[] args) throws IOException {
        new Main().run(args);
    }

    private void run(String[] args) throws IOException {
        parseArgs(args);

        Map<String, Dictionary> dictionaries = loadDictionaries(dataDir);
        if (dictionaries.isEmpty()) {
            System.err.println("no *.corpus files found in " + dataDir.toAbsolutePath());
            System.exit(2);
        }
        String defaultV = defaultVersion != null ? defaultVersion
                : dictionaries.keySet().iterator().next();
        if (!dictionaries.containsKey(defaultV)) {
            System.err.println("default version not found: " + defaultV
                    + ", available: " + dictionaries.keySet());
            System.exit(2);
        }

        SegmentServer server = SegmentServer.start(port, dictionaries, unknownCost, defaultV);
        System.out.println("segmenter service started");
        System.out.println("  listen:      http://127.0.0.1:" + server.port());
        System.out.println("  data dir:    " + dataDir.toAbsolutePath());
        System.out.println("  versions:    " + dictionaries.keySet());
        System.out.println("  default:     " + defaultV);
        System.out.println("  unk cost:    " + unknownCost);
        System.out.println("  POST /segment {\"text\":\"...\",\"n\":1,\"version\":\"" + defaultV + "\"}");

        Runtime.getRuntime().addShutdownHook(new Thread(server::stop));
    }

    private void parseArgs(String[] args) {
        for (int i = 0; i < args.length; i++) {
            String arg = args[i];
            switch (arg) {
                case "--port":
                    port = Integer.parseInt(requireValue(args, ++i, arg));
                    break;
                case "--data-dir":
                    dataDir = Paths.get(requireValue(args, ++i, arg));
                    break;
                case "--unknown-cost":
                    unknownCost = Double.parseDouble(requireValue(args, ++i, arg));
                    break;
                case "--default":
                    defaultVersion = requireValue(args, ++i, arg);
                    break;
                case "-h":
                case "--help":
                    printHelp();
                    System.exit(0);
                default:
                    System.err.println("unknown argument: " + arg);
                    printHelp();
                    System.exit(2);
            }
        }
    }

    private static String requireValue(String[] args, int index, String flag) {
        if (index >= args.length) {
            System.err.println("missing value for " + flag);
            System.exit(2);
        }
        return args[index];
    }

    private static void printHelp() {
        System.out.println("Usage: Main [--port 8080] [--data-dir data/corpora] "
                + "[--unknown-cost 10.0] [--default <version>]");
    }

    private static Map<String, Dictionary> loadDictionaries(Path dir) throws IOException {
        if (!Files.isDirectory(dir)) {
            throw new IOException("data directory not found: " + dir.toAbsolutePath());
        }
        List<Path> files = new ArrayList<>();
        try (DirectoryStream<Path> stream = Files.newDirectoryStream(dir, "*.corpus")) {
            for (Path p : stream) {
                if (Files.isRegularFile(p)) {
                    files.add(p);
                }
            }
        }
        files.sort(Path::compareTo);
        Map<String, Dictionary> dictionaries = new LinkedHashMap<>();
        for (Path file : files) {
            Dictionary dictionary = CorpusLoader.load(file);
            System.out.println("loaded " + file.getFileName() + " as version '"
                    + dictionary.version() + "' (" + dictionary.size() + " words)");
            dictionaries.put(dictionary.version(), dictionary);
        }
        return dictionaries;
    }
}
