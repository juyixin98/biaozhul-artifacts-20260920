package com.bm25stable;

import java.util.Map;

/**
 * 服务入口：装载合成语料并启动 HTTP 服务。
 *
 * <p>用法：java -cp out com.bm25stable.Main [port]
 * <br>端口默认 8080，也可用环境变量 PORT 覆盖。
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        int port = resolvePort(args);

        SearchEngine engine = new SearchEngine();
        Map<String, String> corpus = SyntheticCorpus.documents();
        engine.upsertAll(corpus);
        System.out.printf("loaded synthetic corpus: %d documents, snapshot version %d%n",
                corpus.size(), engine.currentVersion());

        HttpSearchServer server = new HttpSearchServer(engine, port);
        server.start();
        System.out.println("BM25 stable-pagination search server listening on http://localhost:" + server.port());
        System.out.println("try: curl 'http://localhost:" + server.port() + "/search?q=apple&pageSize=3'");

        Runtime.getRuntime().addShutdownHook(new Thread(server::stop));
        Thread.currentThread().join();
    }

    private static int resolvePort(String[] args) {
        if (args.length > 0) {
            return Integer.parseInt(args[0]);
        }
        String env = System.getenv("PORT");
        if (env != null && !env.isBlank()) {
            return Integer.parseInt(env.trim());
        }
        return 8080;
    }
}
