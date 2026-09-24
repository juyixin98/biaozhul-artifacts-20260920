package com.example.segindex.server;

import com.example.segindex.Index;
import com.example.segindex.IndexConfig;

import java.nio.file.Path;

/**
 * CLI entry point.
 *
 * <pre>java -jar segindex.jar [--port 8080] [--data ./index-data] [--merge-factor 4]</pre>
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        int port = 8080;
        String dataDir = "./index-data";
        int mergeFactor = 4;
        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--port" -> port = Integer.parseInt(args[++i]);
                case "--data" -> dataDir = args[++i];
                case "--merge-factor" -> mergeFactor = Integer.parseInt(args[++i]);
                default -> {
                    System.err.println("unknown argument: " + args[i]);
                    System.exit(2);
                }
            }
        }
        IndexConfig config = IndexConfig.builder()
                .mergeFactor(mergeFactor)
                .backgroundMergeEnabled(true)
                .build();
        Index index = Index.open(Path.of(dataDir), config);
        IndexServer server = new IndexServer(index, port);
        server.start();
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            server.stop();
            index.close();
        }));
        System.out.printf("segindex listening on http://localhost:%d (data dir: %s)%n",
                server.port(), Path.of(dataDir).toAbsolutePath());
        Thread.currentThread().join();
    }
}
