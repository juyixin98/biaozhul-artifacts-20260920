package com.example.edcand;

import java.util.List;

/**
 * 命令行入口。
 *
 * <pre>
 *   java com.example.edcand.App serve [port]   # 启动 JSON 服务（默认 8080）
 *   java com.example.edcand.App demo           # 控制台演示若干查询（不启动服务）
 *   java com.example.edcand.App dist A B [NFC|NFD|NFKC|NFKD|NONE]
 *                                              # 直接打印两串的码点距离
 * </pre>
 */
public final class App {

    private App() {
    }

    public static void main(String[] args) throws Exception {
        String mode = args.length == 0 ? "demo" : args[0];
        switch (mode) {
            case "serve" -> {
                int port = args.length >= 2 ? Integer.parseInt(args[1]) : 8080;
                SearchServer server = SearchServer.withDefaultCorpus();
                server.start(port);
                System.out.printf("edit-distance candidate service on http://127.0.0.1:%d%n",
                        server.getPort());
                System.out.println("endpoints: GET /health, GET /stats, POST /distance, POST /search");
            }
            case "demo" -> runDemo();
            case "dist" -> {
                if (args.length < 3) {
                    System.err.println("usage: dist <a> <b> [normalization]");
                    System.exit(2);
                }
                TextNormalization norm = args.length >= 4
                        ? TextNormalization.parse(args[3]) : TextNormalization.NFC;
                String a = norm.apply(args[1]);
                String b = norm.apply(args[2]);
                int d = Levenshtein.distance(a, b);
                System.out.printf("distance=%d  codePoints(a)=%d codePoints(b)=%d  norm=%s%n",
                        d, CodePoints.length(a), CodePoints.length(b), norm);
            }
            default -> {
                System.err.println("unknown mode: " + mode + " (serve|demo|dist)");
                System.exit(2);
            }
        }
    }

    /** 控制台演示：空串、组合字符、长公共前缀、emoji 等场景。 */
    static void runDemo() {
        List<String> corpus = CorpusGenerator.defaultCorpus();
        CandidateIndex index = CandidateIndex.build(corpus, TextNormalization.NFC);
        System.out.printf("corpus terms: %d (q=%d, NFC + case fold)%n%n",
                index.size(), CandidateIndex.Q);

        record Q(String text, int k) {
        }
        List<Q> queries = List.of(
                new Q("", 1),
                new Q("peple", 1),        // 与基础词 people 距离 1
                new Q("café", 1),   // NFD 输入，应与 NFC café 距离 0
                new Q("document_section_paragraph_twon", 2), // 长公共前缀
                new Q("😀", 1),           // emoji，码点距离而非字节距离
                new Q("日本語", 1),
                new Q("abc", 1)           // 全角 ａｂｃ 需 NFKC，NFC 下演示对照
        );
        for (Q q : queries) {
            SearchOutcome out = index.search(q.text(), q.k());
            System.out.printf("query=%s (codepoints=%d) k=%d -> %d match(es); "
                            + "lengthGate=%d candidates=%d exactCalls=%d%n",
                    display(q.text()), CodePoints.length(q.text()), q.k(),
                    out.matches().size(), out.passedLengthGate(),
                    out.candidates(), out.scannedExact());
            for (SearchOutcome.Match m : out.matches()) {
                System.out.printf("    d=%d  %s%n", m.distance(), display(m.term()));
            }
            System.out.println();
        }

        // 规范化对照：全角字符串
        String fw = "ａｂｃ";
        System.out.println("normalization contrast for " + display(fw) + ":");
        for (TextNormalization n : List.of(TextNormalization.NFC, TextNormalization.NFKC)) {
            CandidateIndex idx2 = CandidateIndex.build(corpus, n);
            SearchOutcome o = idx2.search("abc", 1);
            System.out.printf("  %s: %d match(es), first candidates contain full-width? %s%n",
                    n, o.matches().size(),
                    o.matches().stream().anyMatch(m -> m.term().equals("abc") || m.term().equals("ａｂｃ")));
        }
    }

    private static String display(String s) {
        String shown = s.replace("\t", "\\t");
        return "\"" + shown + "\"";
    }
}
