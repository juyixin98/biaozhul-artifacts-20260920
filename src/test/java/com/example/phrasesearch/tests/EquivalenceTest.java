package com.example.phrasesearch.tests;

import com.example.phrasesearch.analyze.Analyzer;
import com.example.phrasesearch.analyze.StandardAnalyzer;
import com.example.phrasesearch.analyze.StopGapAnalyzer;
import com.example.phrasesearch.index.Index;
import com.example.phrasesearch.model.Document;
import com.example.phrasesearch.model.Posting;
import com.example.phrasesearch.query.PhraseMatch;
import com.example.phrasesearch.query.PhraseQuery;
import com.example.phrasesearch.search.BruteForce;
import com.example.phrasesearch.search.Searcher;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Random;

/**
 * 用“穷举参考实现”做差分测试：
 *  1) 对一小组手工构造的小序列做全枚举（所有 slop 0..3、单词/双词/三词、
 *     字段限定与跨字段），逐字段比较命中数与位置序列；
 *  2) 固定种子随机生成大量微型文档（含大量重复词），随机查询，比较两实现。
 *
 * Searcher（倒排+剪枝 DFS）与 BruteForce（笛卡尔积）算法完全独立，
 * 结果（含顺序、slopUsed、crossField）必须逐项一致。
 */
final class EquivalenceTest {

    private EquivalenceTest() {
    }

    static void run() {
        exhaustiveTinySequences(new StandardAnalyzer(), "standard");
        exhaustiveTinySequences(new StopGapAnalyzer(StopGapAnalyzer.DEFAULT_STOPWORDS),
                "stop_gap");
        randomDifferentialStandard();
        randomDifferentialStopGap();
    }

    private static List<String> signature(List<PhraseMatch> matches) {
        List<String> out = new ArrayList<>();
        for (PhraseMatch m : matches) {
            StringBuilder sb = new StringBuilder(m.docExternalId()).append('|');
            for (Posting p : m.postings()) {
                sb.append(p.field()).append(':').append(p.position()).append(',');
            }
            sb.append("|slop=").append(m.slopUsed())
                    .append("|cross=").append(m.crossField());
            out.add(sb.toString());
        }
        return out;
    }

    /** 对给定微型索引，枚举一组查询 × slop × 字段范围，比较两实现。 */
    private static void assertEquivalent(Index index, List<String> queries,
                                         List<String> fields, String tag) {
        Searcher searcher = new Searcher();
        BruteForce brute = new BruteForce();
        for (String phrase : queries) {
            List<String> terms = List.of(phrase.split(" "));
            for (String field : fields) {
                for (int slop = 0; slop <= 3; slop++) {
                    PhraseQuery q = new PhraseQuery(terms, slop, field, true, phrase);
                    List<String> a = signature(searcher.search(index, q));
                    List<String> b = signature(brute.search(index, q));
                    Assert.check("等价性[" + tag + "]: \"" + phrase + "\" field="
                            + (field == null ? "_all" : field) + " slop=" + slop,
                            () -> a.equals(b));
                }
            }
        }
    }

    private static Index tinyIndex(Analyzer analyzer, String tag) {
        Index index = new Index(analyzer);
        LinkedHashMap<String, String> f1 = new LinkedHashMap<>();
        f1.put("f", "a b a b a");
        index.addDocument(new Document("seq_" + tag + "_1", f1));

        LinkedHashMap<String, String> f2 = new LinkedHashMap<>();
        f2.put("f", "a a a");
        index.addDocument(new Document("seq_" + tag + "_2", f2));

        LinkedHashMap<String, String> f3 = new LinkedHashMap<>();
        f3.put("f", "b a b");
        index.addDocument(new Document("seq_" + tag + "_3", f3));
        return index;
    }

    private static Index tinyCrossFieldIndex(Analyzer analyzer, String tag) {
        Index index = new Index(analyzer);
        LinkedHashMap<String, String> f1 = new LinkedHashMap<>();
        f1.put("t", "a b");
        f1.put("b", "a c a");
        index.addDocument(new Document("x_" + tag + "_1", f1));

        LinkedHashMap<String, String> f2 = new LinkedHashMap<>();
        f2.put("t", "the a");
        f2.put("b", "the b");
        index.addDocument(new Document("x_" + tag + "_2", f2));
        return index;
    }

    private static void exhaustiveTinySequences(Analyzer analyzer, String tag) {
        Index single = tinyIndex(analyzer, tag);
        assertEquivalent(single,
                List.of("a", "b", "a a", "a b", "b a", "b b",
                        "a a a", "a b a", "b a b", "a a b", "b a a"),
                List.of("f"),
                tag + "-single-field");

        Index cross = tinyCrossFieldIndex(analyzer, tag);
        assertEquivalent(cross,
                List.of("a", "b", "c", "a a", "a b", "b a", "a c",
                        "c a", "a a a", "b a c", "a b a", "the a", "the b", "the the"),
                // List.of 不允许 null 元素；null 表示跨字段（不指定字段）
                java.util.Arrays.asList("t", "b", null),
                tag + "-cross-field");
    }

    private static void randomDifferential(Analyzer analyzer, String tag) {
        String[] vocab = {"a", "b", "the", "x", "y", "a", "b", "the"}; // 加权重复
        Random rnd = new Random(4242 + tag.hashCode());
        Searcher searcher = new Searcher();
        BruteForce brute = new BruteForce();

        int comparisons = 0;
        for (int iter = 0; iter < 60; iter++) {
            Index index = new Index(analyzer);
            int docCount = 1 + rnd.nextInt(4);
            for (int d = 0; d < docCount; d++) {
                LinkedHashMap<String, String> fields = new LinkedHashMap<>();
                int fieldCount = 1 + rnd.nextInt(3);
                for (int fi = 0; fi < fieldCount; fi++) {
                    fields.put("f" + fi, randomSentence(rnd, vocab));
                }
                index.addDocument(new Document("r" + iter + "_" + d, fields));
            }

            for (int qi = 0; qi < 20; qi++) {
                int len = 1 + rnd.nextInt(4);
                List<String> rawTerms = new ArrayList<>();
                for (int k = 0; k < len; k++) {
                    rawTerms.add(vocab[rnd.nextInt(vocab.length)]);
                }
                // 与真实服务一致：查询先过分析器；stop_gap 下全停用词查询会变空，跳过
                List<String> terms = analyzer.analyze("_q", String.join(" ", rawTerms))
                        .tokens().stream().map(com.example.phrasesearch.model.Token::term).toList();
                if (terms.isEmpty()) {
                    continue;
                }
                String field = switch (rnd.nextInt(3)) {
                    case 0 -> null;
                    case 1 -> "f0";
                    default -> "f1"; // 单字段文档上会自然变空，两实现同样为空
                };
                int slop = rnd.nextInt(4);
                PhraseQuery q = new PhraseQuery(terms, slop, field, true, String.join(" ", terms));
                List<String> a = signature(searcher.search(index, q));
                List<String> b = signature(brute.search(index, q));
                comparisons++;
                List<String> aFinal = a;
                List<String> bFinal = b;
                Assert.check("随机差分[" + tag + "] iter=" + iter + " q=" + terms
                                + " field=" + field + " slop=" + slop,
                        () -> aFinal.equals(bFinal));
            }
        }
        // stop_gap 下部分全停用词查询被跳过，因此只要求下界；两模式下都应有大量比较
        Assert.isTrue("随机差分[" + tag + "] 比较次数 >= 900，实际 " + comparisons,
                comparisons >= 900);
    }

    private static String randomSentence(Random rnd, String[] vocab) {
        int len = rnd.nextInt(9); // 允许空字段
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < len; i++) {
            if (i > 0) {
                sb.append(rnd.nextBoolean() ? " " : ", ");
            }
            sb.append(vocab[rnd.nextInt(vocab.length)]);
        }
        return sb.toString();
    }

    private static void randomDifferentialStandard() {
        randomDifferential(new StandardAnalyzer(), "standard");
    }

    private static void randomDifferentialStopGap() {
        randomDifferential(new StopGapAnalyzer(StopGapAnalyzer.DEFAULT_STOPWORDS), "stopgap");
    }
}
