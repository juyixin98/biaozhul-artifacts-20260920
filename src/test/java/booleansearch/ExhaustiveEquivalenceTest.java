package booleansearch;

import booleansearch.index.InvertedIndex;
import booleansearch.model.Document;
import booleansearch.query.QueryNode;
import booleansearch.query.QueryParser;
import booleansearch.search.EvalResult;
import booleansearch.search.NaiveEvaluator;
import booleansearch.search.OptimizedPlanner;

import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;

import static booleansearch.TestFramework.assertEquals;
import static booleansearch.TestFramework.assertTrue;

/**
 * 验收核心：穷举小文档集合，对照独立的集合运算真值。
 *
 * <p>做法：
 * <ol>
 *   <li>4 篇文档、3 个词项 a/b/c（外加一个索引里根本不存在的未知词 z），
 *       文档-词矩阵为 {@code 1={a,b}, 2={b,c}, 3={a,c}, 4={a}}；</li>
 *   <li>穷举 2^4=16 种删除子集（NOT 的固定全集随之变化，覆盖删除文档语义）；</li>
 *   <li>穷举深度 ≤ 2 的全部查询形态：纯 NOT、NOT NOT、未知词参与、括号组合，
 *       共 {@link #generateQueries()} 个；</li>
 *   <li>每个 (删除子集, 查询) 组合都比较：朴素求值 / 优化求值 /
 *       <b>独立真值</b>（不经过本项目的索引与求值器，直接由文档-词矩阵做
 *       JDK 集合运算）三者必须完全一致；</li>
 *   <li>同时断言优化计划的探测次数 ≤ 朴素计划，并统计严格减少的查询数。</li>
 * </ol>
 */
public final class ExhaustiveEquivalenceTest {

    /** 文档 id -> 该文档包含的词（文档-词矩阵，真值的唯一数据来源）。 */
    private static final List<List<String>> DOC_TERMS = List.of(
            List.of("a", "b"),
            List.of("b", "c"),
            List.of("a", "c"),
            List.of("a")
    );

    private static final String[] ATOMS = {"a", "b", "c", "z"};

    public static void run() {
        TestFramework.reset();
        TestFramework.section("ExhaustiveEquivalenceTest: 生成查询");

        List<String> queryStrings = generateQueries();
        List<QueryNode> trees = new ArrayList<>();
        for (String q : queryStrings) {
            trees.add(QueryParser.parse(q));
        }
        System.out.println("   查询形态数: " + queryStrings.size()
                + "，删除子集数: 16，比较组合数: " + (queryStrings.size() * 16L));

        long comparisons = 0;
        long strictSavingsQueries = 0;
        long totalNaiveProbes = 0;
        long totalOptimizedProbes = 0;

        for (int mask = 0; mask < (1 << DOC_TERMS.size()); mask++) {
            InvertedIndex index = buildIndexWithDeletes(mask);
            Set<Integer> deleted = deletedIds(mask);
            TreeSet<Integer> universe = universe(mask);

            for (int qi = 0; qi < trees.size(); qi++) {
                QueryNode tree = trees.get(qi);

                EvalResult naive = new NaiveEvaluator(index).evaluate(tree);
                EvalResult optimized = new OptimizedPlanner(index).evaluate(tree);
                TreeSet<Integer> truth = groundTruth(tree, deleted, universe);

                comparisons++;
                String ctx = "mask=" + mask + " query=\"" + queryStrings.get(qi) + "\"";
                assertEquals(truth, naive.docIds(), "朴素结果 == 真值，" + ctx);
                assertEquals(truth, optimized.docIds(), "优化结果 == 真值，" + ctx);
                assertEquals(naive.docIds(), optimized.docIds(), "优化结果 == 朴素结果，" + ctx);
                assertTrue(optimized.membershipProbes() <= naive.membershipProbes(),
                        "优化探测数(" + optimized.membershipProbes()
                                + ") 不应超过朴素(" + naive.membershipProbes() + ")，" + ctx);

                totalNaiveProbes += naive.membershipProbes();
                totalOptimizedProbes += optimized.membershipProbes();
                if (optimized.membershipProbes() < naive.membershipProbes()) {
                    strictSavingsQueries++;
                }
            }
        }

        System.out.println("   完成比较组合: " + comparisons);
        System.out.println("   朴素探测总数:   " + totalNaiveProbes);
        System.out.println("   优化探测总数:   " + totalOptimizedProbes
                + "（节省 " + (totalNaiveProbes - totalOptimizedProbes) + " 次）");
        System.out.println("   严格减少探测的(查询,删除子集)组合数: " + strictSavingsQueries);
        assertTrue(comparisons == (long) queryStrings.size() * 16, "比较组合数符合预期");
        assertTrue(strictSavingsQueries > 0, "必须存在优化后严格减少探测次数的情形");
        assertTrue(totalOptimizedProbes < totalNaiveProbes, "总体探测数优化后应更少");

        // 纯 NOT、未知词、删除文档三个指定场景再做点名断言
        spotChecks();

        boolean ok = TestFramework.finish();
        if (!ok) {
            throw new AssertionError("ExhaustiveEquivalenceTest 存在失败");
        }
    }

    /** 生成深度 ≤ 2 的查询：原子 + NOT 原子 + 二元深度2 + NOT 深度1。 */
    static List<String> generateQueries() {
        List<String> depth0 = new ArrayList<>(List.of(ATOMS));
        List<String> depth1 = new ArrayList<>(depth0);
        // 纯 NOT（含 NOT NOT）
        for (String a : depth0) {
            depth1.add("NOT " + a);
        }
        // 深度 1 二元（全括号，左/右原子）
        for (String left : depth0) {
            for (String right : depth0) {
                depth1.add("(" + left + " AND " + right + ")");
                depth1.add("(" + left + " OR " + right + ")");
            }
        }
        List<String> depth2 = new ArrayList<>(depth1);
        // 深度 2：(depth1二元) AND/OR 原子，及 原子 AND/OR (depth1二元)
        List<String> pairs = new ArrayList<>();
        for (String left : depth0) {
            for (String right : depth0) {
                pairs.add("(" + left + " AND " + right + ")");
                pairs.add("(" + left + " OR " + right + ")");
            }
        }
        for (String p : pairs) {
            for (String a : depth0) {
                depth2.add("(" + p + " AND " + a + ")");
                depth2.add("(" + p + " OR " + a + ")");
                depth2.add("(" + a + " AND " + p + ")");
                depth2.add("(" + a + " OR " + p + ")");
            }
        }
        for (String d1 : depth1) {
            if (!depth0.contains(d1)) {
                depth2.add("NOT (" + d1 + ")");
            }
        }
        return depth2;
    }

    // ---------- 独立真值：不经索引，直接由文档-词矩阵做集合运算 ----------

    private static TreeSet<Integer> groundTruth(QueryNode node, Set<Integer> deleted,
                                                TreeSet<Integer> universe) {
        return switch (node) {
            case QueryNode.Term t -> {
                TreeSet<Integer> docs = new TreeSet<>();
                for (int docId = 1; docId <= DOC_TERMS.size(); docId++) {
                    if (DOC_TERMS.get(docId - 1).contains(t.term()) && !deleted.contains(docId)) {
                        docs.add(docId);
                    }
                }
                yield docs;
            }
            case QueryNode.Not n -> {
                TreeSet<Integer> child = groundTruth(n.child(), deleted, universe);
                TreeSet<Integer> result = new TreeSet<>(universe);
                result.removeAll(child);
                yield result;
            }
            case QueryNode.And a -> {
                TreeSet<Integer> result = new TreeSet<>(universe);
                result.retainAll(groundTruth(a.children().get(0), deleted, universe));
                for (int i = 1; i < a.children().size(); i++) {
                    result.retainAll(groundTruth(a.children().get(i), deleted, universe));
                }
                yield result;
            }
            case QueryNode.Or o -> {
                TreeSet<Integer> result = new TreeSet<>();
                for (QueryNode child : o.children()) {
                    result.addAll(groundTruth(child, deleted, universe));
                }
                yield result;
            }
        };
    }

    private static InvertedIndex buildIndexWithDeletes(int mask) {
        InvertedIndex index = new InvertedIndex();
        for (int docId = 1; docId <= DOC_TERMS.size(); docId++) {
            String text = String.join(" ", DOC_TERMS.get(docId - 1));
            index.putDocument(new Document(docId, "doc" + docId, text));
        }
        for (int docId = 1; docId <= DOC_TERMS.size(); docId++) {
            if ((mask & (1 << (docId - 1))) != 0) {
                index.delete(docId);
            }
        }
        return index;
    }

    private static Set<Integer> deletedIds(int mask) {
        Set<Integer> deleted = new TreeSet<>();
        for (int docId = 1; docId <= DOC_TERMS.size(); docId++) {
            if ((mask & (1 << (docId - 1))) != 0) {
                deleted.add(docId);
            }
        }
        return deleted;
    }

    private static TreeSet<Integer> universe(int mask) {
        TreeSet<Integer> universe = new TreeSet<>();
        for (int docId = 1; docId <= DOC_TERMS.size(); docId++) {
            if ((mask & (1 << (docId - 1))) == 0) {
                universe.add(docId);
            }
        }
        return universe;
    }

    /** 任务中点名的三类场景：纯 NOT、未知词、删除文档。 */
    private static void spotChecks() {
        TestFramework.section("ExhaustiveEquivalenceTest: 点名场景");

        InvertedIndex idx = buildIndexWithDeletes(0);
        // 纯 NOT：NOT a == 全集减去 a 的链
        EvalResult notA = new NaiveEvaluator(idx).evaluate(QueryParser.parse("NOT a"));
        assertEquals(new TreeSet<>(List.of(2)), notA.docIds(), "无删除时 NOT a = {2}");

        // 未知词：z 为空链；NOT z 是整个全集
        EvalResult notZ = new OptimizedPlanner(idx).evaluate(QueryParser.parse("NOT z"));
        assertEquals(new TreeSet<>(List.of(1, 2, 3, 4)), notZ.docIds(), "NOT 未知词 = 全部存活文档");
        EvalResult aAndZ = new NaiveEvaluator(idx).evaluate(QueryParser.parse("a AND z"));
        assertEquals(new TreeSet<Integer>(), aAndZ.docIds(), "a AND 未知词 = 空");
        EvalResult aOrZ = new NaiveEvaluator(idx).evaluate(QueryParser.parse("a OR z"));
        assertEquals(new TreeSet<>(List.of(1, 3, 4)), aOrZ.docIds(), "a OR 未知词 = a");

        // 删除文档后：删除 2（含 b,c），NOT a 变成 { }，删除 4 后 NOT a 只剩 {2}
        idx.delete(2);
        EvalResult notAAfter = new NaiveEvaluator(idx).evaluate(QueryParser.parse("NOT a"));
        assertEquals(new TreeSet<Integer>(), notAAfter.docIds(),
                "删除唯一不含 a 的文档 2 后，NOT a = 空");
        EvalResult bAfter = new NaiveEvaluator(idx).evaluate(QueryParser.parse("b"));
        assertEquals(new TreeSet<>(List.of(1)), bAfter.docIds(), "删除 2 后 b 的链只剩 {1}");

        InvertedIndex idx2 = buildIndexWithDeletes(0);
        idx2.delete(4);
        EvalResult notA2 = new OptimizedPlanner(idx2).evaluate(QueryParser.parse("NOT a"));
        assertEquals(new TreeSet<>(List.of(2)), notA2.docIds(), "删除 4 不影响 NOT a，仍为 {2}");
        EvalResult zAfterDelete = new NaiveEvaluator(idx2).evaluate(QueryParser.parse("NOT z"));
        assertEquals(new TreeSet<>(List.of(1, 2, 3)), zAfterDelete.docIds(),
                "删除文档后 NOT 未知词的全集随之缩小");
    }
}
