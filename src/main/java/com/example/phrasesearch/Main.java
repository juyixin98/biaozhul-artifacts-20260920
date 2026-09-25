package com.example.phrasesearch;

import com.example.phrasesearch.analyze.Analyzer;
import com.example.phrasesearch.analyze.StandardAnalyzer;
import com.example.phrasesearch.analyze.StopGapAnalyzer;
import com.example.phrasesearch.corpus.SyntheticCorpus;
import com.example.phrasesearch.index.Index;
import com.example.phrasesearch.model.Document;
import com.example.phrasesearch.server.PhraseHttpServer;
import com.example.phrasesearch.service.PhraseService;

import java.util.List;

/**
 * 服务入口。
 *
 * 用法：
 *   java -cp build/classes com.example.phrasesearch.Main [--port 8080] [--analyzer standard|stop_gap]
 *
 * 默认加载内置合成语料；--corpus file.json 可加载外部语料文件，格式：
 * {"documents":[{"id":"x","fields":{"title":"...","body":"..."}}]}
 */
public final class Main {

    public static void main(String[] args) throws Exception {
        int port = 8080;
        String analyzerName = "standard";
        String corpusPath = null;

        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--port" -> port = Integer.parseInt(requireValue(args, ++i, "--port"));
                case "--analyzer" -> analyzerName = requireValue(args, ++i, "--analyzer");
                case "--corpus" -> corpusPath = requireValue(args, ++i, "--corpus");
                case "--help", "-h" -> {
                    System.out.println("Usage: Main [--port 8080] "
                            + "[--analyzer standard|stop_gap] [--corpus path.json]");
                    return;
                }
                default -> throw new IllegalArgumentException("unknown argument: " + args[i]);
            }
        }

        Analyzer analyzer = switch (analyzerName) {
            case "standard" -> new StandardAnalyzer();
            case "stop_gap" -> new StopGapAnalyzer(StopGapAnalyzer.DEFAULT_STOPWORDS);
            default -> throw new IllegalArgumentException(
                    "unknown analyzer: " + analyzerName + " (expected standard|stop_gap)");
        };

        Index index = new Index(analyzer);
        List<Document> docs = corpusPath == null
                ? SyntheticCorpus.documents()
                : CorpusLoader.load(corpusPath);
        for (Document d : docs) {
            index.addDocument(d);
        }

        PhraseService service = new PhraseService(index);
        PhraseHttpServer http = new PhraseHttpServer(service);
        http.start(port);

        System.out.println("Phrase position search service started");
        System.out.println("  analyzer : " + analyzer.name());
        System.out.println("  docs     : " + index.size());
        System.out.println("  port     : " + http.getPort());
        System.out.println("  endpoints: GET /health /config /docs ; "
                + "GET|POST /search ; GET|POST /analyze");

        // 让进程随服务存活；收到 Ctrl+C 时干净退出
        Runtime.getRuntime().addShutdownHook(new Thread(http::stop));
        Thread.currentThread().join();
    }

    private static String requireValue(String[] args, int idx, String flag) {
        if (idx >= args.length) {
            throw new IllegalArgumentException("missing value for " + flag);
        }
        return args[idx];
    }
}
