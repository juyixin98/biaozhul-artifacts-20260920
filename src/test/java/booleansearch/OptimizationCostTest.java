package booleansearch;

import booleansearch.corpus.SyntheticCorpus;
import booleansearch.index.InvertedIndex;
import booleansearch.query.QueryParser;
import booleansearch.search.EvalResult;
import booleansearch.search.NaiveEvaluator;
import booleansearch.search.OptimizedPlanner;

import static booleansearch.TestFramework.assertEquals;
import static booleansearch.TestFramework.assertTrue;

/**
 * 优化前后成本对照：在真实合成语料上，构造倒排链大小差异显著的 AND 查询，
 * 断言"小集合优先"的优化计划严格减少成员探测次数，同时结果保持一致。
 */
public final class OptimizationCostTest {

    public static void run() {
        TestFramework.reset();
        TestFramework.section("OptimizationCostTest: 优化前后探测次数对照");

        InvertedIndex idx = new InvertedIndex();
        SyntheticCorpus.loadInto(idx);

        // 差序输入：大链 -> 中链 -> 稀有链（df: pet=7, cat=6, zebra=1）
        String worstFirst = "pet AND cat AND zebra";
        EvalResult naiveWorst = new NaiveEvaluator(idx).evaluate(QueryParser.parse(worstFirst));
        EvalResult optWorst = new OptimizedPlanner(idx).evaluate(QueryParser.parse(worstFirst));
        report(worstFirst, naiveWorst, optWorst);
        assertEquals(naiveWorst.docIds(), optWorst.docIds(), "差序查询结果一致");
        assertTrue(optWorst.membershipProbes() < naiveWorst.membershipProbes(),
                "差序查询优化后探测应严格更少");
        // 朴素：先 pet(7)∩cat(6) 探测 7 次得 {1,3,6,7,12}(5篇)，再 ∩zebra 探测 5 次 = 12
        // 优化：先 zebra(1)，∩cat 探测 1 次即为空，空集 ∩pet 探测 0 次 = 1
        assertEquals(12L, naiveWorst.membershipProbes(), "朴素差序探测次数可精确预测");
        assertEquals(1L, optWorst.membershipProbes(), "优化后探测次数可精确预测");

        // 混合：常见词在前，括号子表达式在后
        String mixed = "pet AND (falcon OR glacier)";
        EvalResult naiveMixed = new NaiveEvaluator(idx).evaluate(QueryParser.parse(mixed));
        EvalResult optMixed = new OptimizedPlanner(idx).evaluate(QueryParser.parse(mixed));
        report(mixed, naiveMixed, optMixed);
        assertEquals(naiveMixed.docIds(), optMixed.docIds(), "混合查询结果一致");
        assertTrue(optMixed.membershipProbes() < naiveMixed.membershipProbes(),
                "混合查询优化后探测应严格更少（子表达式结果小被提前）");

        // 链大小相同的并列场景：不应变慢
        String tied = "cat AND dog AND food";
        EvalResult naiveTied = new NaiveEvaluator(idx).evaluate(QueryParser.parse(tied));
        EvalResult optTied = new OptimizedPlanner(idx).evaluate(QueryParser.parse(tied));
        report(tied, naiveTied, optTied);
        assertEquals(naiveTied.docIds(), optTied.docIds(), "同尺寸查询结果一致");
        assertTrue(optTied.membershipProbes() <= naiveTied.membershipProbes(),
                "同尺寸时优化不应更差");

        // 纯 NOT / OR：AND 优化不改变它们的工作量
        String notOnly = "NOT zebra";
        EvalResult n1 = new NaiveEvaluator(idx).evaluate(QueryParser.parse(notOnly));
        EvalResult n2 = new OptimizedPlanner(idx).evaluate(QueryParser.parse(notOnly));
        assertEquals(n1.membershipProbes(), n2.membershipProbes(), "纯 NOT 两种计划工作量相同");
        assertEquals(n1.docIds(), n2.docIds(), "纯 NOT 结果相同");

        boolean ok = TestFramework.finish();
        if (!ok) {
            throw new AssertionError("OptimizationCostTest 存在失败");
        }
    }

    private static void report(String query, EvalResult naive, EvalResult optimized) {
        System.out.printf("   %-32s 朴素探测=%-3d 优化探测=%-3d 节省=%d 命中=%s%n",
                query, naive.membershipProbes(), optimized.membershipProbes(),
                naive.membershipProbes() - optimized.membershipProbes(), optimized.docIds());
    }
}
