package com.bm25pager;

import com.bm25pager.api.ApiServer;
import com.bm25pager.corpus.SyntheticCorpus;
import com.bm25pager.index.IndexManager;
import com.bm25pager.model.Document;

import java.util.List;

/**
 * 服务入口。
 *
 * 启动时用确定性的 {@link SyntheticCorpus} 初始化索引并发布 v1 快照，
 * 然后启动仅依赖 JDK 的 HTTP JSON 服务。
 *
 * 环境变量（可选）：
 *   BM25_PORT             监听端口，默认 8080
 *   BM25_RETAIN_SNAPSHOTS 保留的历史快照数，默认 5
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        int port = envInt("BM25_PORT", 8080);
        int retain = envInt("BM25_RETAIN_SNAPSHOTS", 5);

        IndexManager indexManager = new IndexManager(retain);
        List<Document> corpus = SyntheticCorpus.create();
        indexManager.loadInitial(corpus);

        ApiServer apiServer = new ApiServer(indexManager, port);
        apiServer.start();

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            System.out.println("Shutting down…");
            apiServer.stop();
        }));

        System.out.println("==========================================================");
        System.out.println(" BM25 stable-pagination service");
        System.out.println(" Listen port     : " + apiServer.port());
        System.out.println(" Corpus documents: " + corpus.size());
        System.out.println(" Current version : " + indexManager.currentVersion());
        System.out.println(" Retain snapshots: " + retain);
        System.out.println(" Health check    : GET  http://localhost:" + apiServer.port() + "/health");
        System.out.println(" Search          : POST http://localhost:" + apiServer.port() + "/search");
        System.out.println("==========================================================");
    }

    private static int envInt(String name, int fallback) {
        String raw = System.getenv(name);
        if (raw == null || raw.isBlank()) {
            return fallback;
        }
        try {
            return Integer.parseInt(raw.trim());
        } catch (NumberFormatException nfe) {
            System.err.println("Invalid integer for env " + name + "=" + raw
                    + ", falling back to " + fallback);
            return fallback;
        }
    }
}
