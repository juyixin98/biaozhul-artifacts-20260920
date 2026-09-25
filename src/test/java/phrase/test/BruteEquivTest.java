package phrase.test;

import phrase.core.Analyzer;
import phrase.core.Analyzers;
import phrase.doc.Corpus;
import phrase.doc.Doc;
import phrase.index.InvertedIndex;
import phrase.search.BruteForceReference;
import phrase.search.Match;
import phrase.search.PhraseMatcher;
import phrase.search.PhraseQuery;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Random;

/**
 * 生产匹配器 vs 蛮力穷举参考的等价性测试：
 * 在随机小序列、随机查询、随机 slop 上逐元组比较。
 */
public final class BruteEquivTest {

    public static void register(TestRunner runner) {
        runner.add("equivalence/random-small-sequences-vs-brute-force",
                BruteEquivTest::randomEquivalence);
        runner.add("equivalence/corpus-queries-vs-brute-force",
                BruteEquivTest::corpusEquivalence);
    }

    private static void randomEquivalence() {
        Random rnd = new Random(20260924L);
        String[] vocab = {"a", "b", "c"};
        PhraseMatcher matcher = new PhraseMatcher(100_000);
        for (int iter = 0; iter < 300; iter++) {
            int len = 1 + rnd.nextInt(7);
            List<String> toks = new ArrayList<>();
            for (int i = 0; i < len; i++) {
                toks.add(vocab[rnd.nextInt(vocab.length)]);
            }
            Doc doc = Doc.of("rand", "body", String.join(" ", toks));
            InvertedIndex idx = InvertedIndex.build(List.of(doc), Analyzers.standard(), 0);

            int qlen = 1 + rnd.nextInt(3);
            List<String> qterms = new ArrayList<>();
            for (int i = 0; i < qlen; i++) {
                qterms.add(vocab[rnd.nextInt(vocab.length)]);
            }
            int slop = rnd.nextInt(4);
            PhraseQuery q = PhraseQuery.of(qterms, slop);

            List<int[]> fast = matcher.find(idx, "rand", q).stream()
                    .map(m -> m.positions().stream().mapToInt(Integer::intValue).toArray())
                    .toList();
            List<int[]> brute = BruteForceReference.enumerate(idx, "rand", q);
            if (fast.size() != brute.size()) {
                Asserts.fail("iter " + iter + " q=" + qterms + " slop=" + slop
                        + " seq=" + toks + " fast=" + fast.size() + " brute=" + brute.size());
            }
            for (int i = 0; i < brute.size(); i++) {
                if (!Arrays.equals(fast.get(i), brute.get(i))) {
                    Asserts.fail("iter " + iter + " tuple mismatch at " + i
                            + " fast=" + Arrays.toString(fast.get(i))
                            + " brute=" + Arrays.toString(brute.get(i)));
                }
            }
        }
    }

    private static void corpusEquivalence() {
        String[][] queries = {
                {"echo", "echo"}, {"data", "pipeline"}, {"rain", "rain"},
                {"alpha", "alpha", "beta"}, {"cat", "mat"}, {"a", "b", "a"},
                {"blue", "ocean"}, {"the", "cat"}, {"pipeline"}, {"echo", "chamber"}
        };
        PhraseMatcher matcher = new PhraseMatcher(100_000);
        for (Analyzer a : List.of(Analyzers.standard(), Analyzers.stopword())) {
            for (int gap : new int[] {0, 1, 2}) {
                InvertedIndex idx = InvertedIndex.build(Corpus.synthetic(), a, gap);
                for (String[] qt : queries) {
                    for (int slop = 0; slop <= 3; slop++) {
                        PhraseQuery q = PhraseQuery.of(Arrays.asList(qt), slop);
                        for (String docId : idx.docIds()) {
                            List<Match> fast = matcher.find(idx, docId, q);
                            List<int[]> brute = BruteForceReference.enumerate(idx, docId, q);
                            Asserts.assertEquals(brute.size(), fast.size(),
                                    "count [" + a.name() + " gap=" + gap + " q="
                                            + Arrays.toString(qt) + " slop=" + slop
                                            + " doc=" + docId + "]");
                            for (int i = 0; i < brute.size(); i++) {
                                int[] fp = fast.get(i).positions().stream()
                                        .mapToInt(Integer::intValue).toArray();
                                Asserts.assertEquals(
                                        Arrays.stream(brute.get(i)).boxed().toList(), fp,
                                        "tuple equality in " + docId);
                            }
                        }
                    }
                }
            }
        }
    }
}
