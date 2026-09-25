package boolsearch;

import boolsearch.TestRunner.Case;
import boolsearch.eval.Evaluator;
import boolsearch.query.Node;
import boolsearch.query.Parser;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;
import java.util.SortedSet;

/** 查询计划优化测试：交集顺序按倒排大小升序；优化前后结果一致。 */
public final class OptimizeTests {

    static void register(List<Case> cases) {
        cases.add(new Case("optimize: AND 按倒排大小升序求交", OptimizeTests::intersectOrder));
        cases.add(new Case("optimize: 朴素计划按书写顺序求交", OptimizeTests::naiveOrder));
        cases.add(new Case("optimize: 合成语料上优化前后结果一致（500 随机查询）",
                OptimizeTests::equivalenceOnCorpus));
    }

    /** 构造 df 差异明显的索引：big=100, mid=10, small=3。 */
    private static InvertedIndex skewed() {
        InvertedIndex idx = new InvertedIndex();
        for (int i = 1; i <= 100; i++) {
            StringBuilder sb = new StringBuilder("big");
            if (i <= 10) sb.append(" mid");
            if (i <= 3) sb.append(" small");
            idx.addDocument(i, sb.toString());
        }
        return idx;
    }

    private static List<String> traceOf(InvertedIndex idx, String q, boolean optimize) throws Exception {
        Node ast = Parser.parse(q);
        List<String> trace = new ArrayList<>();
        Evaluator ev = new Evaluator(idx, optimize);
        ev.setTrace(trace);
        ev.eval(ast);
        return trace;
    }

    static void intersectOrder() throws Exception {
        List<String> trace = traceOf(skewed(), "big AND small AND mid", true);
        Check.eq(trace.size(), 1, "应有一条 AND 计划记录");
        Check.isTrue(trace.get(0).contains("[3, 10, 100]"),
                "优化计划应按大小升序求交，实际: " + trace.get(0));
    }

    static void naiveOrder() throws Exception {
        List<String> trace = traceOf(skewed(), "big AND small AND mid", false);
        Check.eq(trace.size(), 1, "应有一条 AND 计划记录");
        Check.isTrue(trace.get(0).contains("[100, 3, 10]"),
                "朴素计划应按书写顺序求交，实际: " + trace.get(0));
    }

    static void equivalenceOnCorpus() throws Exception {
        InvertedIndex idx = new InvertedIndex();
        Corpus.generate(300, 99L).forEach(idx::addDocument);
        String[] vocab = Corpus.VOCAB;
        Random r = new Random(2026L);
        Evaluator naive = new Evaluator(idx, false);
        Evaluator opt = new Evaluator(idx, true);
        for (int i = 0; i < 500; i++) {
            Node q = randomQuery(r, vocab, 4);
            SortedSet<Integer> a = naive.eval(q);
            SortedSet<Integer> b = opt.eval(q);
            Check.eq(b, a, "优化前后结果不一致: " + q);
        }
    }

    private static Node randomQuery(Random r, String[] vocab, int depth) {
        if (depth == 0 || r.nextInt(3) == 0) {
            // 偶尔生成未知词
            if (r.nextInt(20) == 0) return new Node.Term("nosuchterm");
            return new Node.Term(vocab[r.nextInt(vocab.length)]);
        }
        return switch (r.nextInt(3)) {
            case 0 -> new Node.Not(randomQuery(r, vocab, depth - 1));
            case 1 -> new Node.And(List.of(randomQuery(r, vocab, depth - 1),
                    randomQuery(r, vocab, depth - 1), randomQuery(r, vocab, depth - 1)));
            default -> new Node.Or(List.of(randomQuery(r, vocab, depth - 1),
                    randomQuery(r, vocab, depth - 1)));
        };
    }
}
