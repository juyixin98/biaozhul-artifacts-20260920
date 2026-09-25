package phrase.test;

import phrase.core.Analyzers;
import phrase.doc.Doc;
import phrase.index.InvertedIndex;
import phrase.search.BruteForceReference;
import phrase.search.Match;
import phrase.search.PhraseMatcher;
import phrase.search.PhraseQuery;

import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 短语匹配器测试：在小型合成序列上穷举验证，
 * 重点覆盖重复词、slop 语义、贪心算法陷阱与结果上限。
 */
public final class MatcherTest {

    public static void register(TestRunner runner) {
        runner.add("matcher/single-term-lists-all-positions", MatcherTest::singleTerm);
        runner.add("matcher/slop0-requires-adjacent-positions", MatcherTest::slop0);
        runner.add("matcher/slop1-allows-one-intervening-token", MatcherTest::slop1);
        runner.add("matcher/repeated-words-never-reuse-same-position",
                MatcherTest::repeatedNoReuse);
        runner.add("matcher/greedy-trap-sequence-alpha-alpha-beta",
                MatcherTest::greedyTrap);
        runner.add("matcher/aba-exhaustive-enumeration", MatcherTest::abaEnumeration);
        runner.add("matcher/result-limit-truncates", MatcherTest::limit);
        runner.add("matcher/missing-term-yields-no-match", MatcherTest::missingTerm);
    }

    /** 单文档（单字段 body）小序列索引。 */
    private static InvertedIndex index(String body) {
        return InvertedIndex.build(List.of(Doc.of("x", "body", body)),
                Analyzers.standard(), 0);
    }

    private static List<Match> find(InvertedIndex idx, String terms, int slop) {
        return new PhraseMatcher(1000).find(idx, "x",
                PhraseQuery.of(Arrays.asList(terms.split(" ")), slop));
    }

    private static List<int[]> tuples(List<Match> ms) {
        return ms.stream().map(m -> m.positions().stream().mapToInt(Integer::intValue).toArray())
                .toList();
    }

    private static void singleTerm() {
        InvertedIndex idx = index("echo echo echo");
        List<int[]> t = tuples(find(idx, "echo", 0));
        Asserts.assertEquals(3, t.size(), "single term returns each position");
        Asserts.assertEquals(List.of(1), Arrays.stream(t.get(0)).boxed().toList(), "p1");
        Asserts.assertEquals(List.of(2), Arrays.stream(t.get(1)).boxed().toList(), "p2");
        Asserts.assertEquals(List.of(3), Arrays.stream(t.get(2)).boxed().toList(), "p3");
    }

    private static void slop0() {
        InvertedIndex idx = index("a b c b a");
        // "a b" slop0: 位置 1-2 精确相邻；位置 5 的 a 后面没有 b
        List<int[]> t = tuples(find(idx, "a b", 0));
        Asserts.assertEquals(1, t.size(), "exactly one adjacent a b");
        Asserts.assertEquals(List.of(1, 2), Arrays.stream(t.get(0)).boxed().toList(), "(1,2)");

        // "b a" slop0: 只有 4-5
        List<int[]> ba = tuples(find(idx, "b a", 0));
        Asserts.assertEquals(1, ba.size(), "one adjacent b a");
        Asserts.assertEquals(List.of(4, 5), Arrays.stream(ba.get(0)).boxed().toList(), "(4,5)");

        // 无相邻序列
        Asserts.assertEquals(0, find(index("a x b"), "a b", 0).size(),
                "a x b does not match a b at slop0");
    }

    private static void slop1() {
        // a x b：中间 1 个词 -> slop1 命中，slop0 不命中
        InvertedIndex idx = index("a x b");
        Asserts.assertEquals(0, find(idx, "a b", 0).size(), "slop0 no");
        List<int[]> t = tuples(find(idx, "a b", 1));
        Asserts.assertEquals(1, t.size(), "slop1 yes");
        Asserts.assertEquals(List.of(1, 3), Arrays.stream(t.get(0)).boxed().toList(), "(1,3)");

        // a x x b：中间 2 个词 -> slop1 不命中，slop2 命中
        InvertedIndex idx2 = index("a x x b");
        Asserts.assertEquals(0, find(idx2, "a b", 1).size(), "two gaps > slop1");
        Asserts.assertEquals(1, find(idx2, "a b", 2).size(), "two gaps == slop2");
    }

    private static void repeatedNoReuse() {
        // 同一位置不能被重复词重用："echo" 只有一个出现时 "echo echo" 必须无命中
        InvertedIndex one = index("echo");
        Asserts.assertEquals(0, find(one, "echo echo", 0).size(),
                "single occurrence cannot satisfy repeated term at slop0");
        Asserts.assertEquals(0, find(one, "echo echo", 5).size(),
                "single occurrence cannot satisfy repeated term even with large slop "
                        + "(positions must be strictly increasing)");

        // 两个出现：恰好一种组合 (1,2)
        InvertedIndex two = index("echo echo");
        List<int[]> t = tuples(find(two, "echo echo", 0));
        Asserts.assertEquals(1, t.size(), "two occurrences -> one pair");
        Asserts.assertEquals(List.of(1, 2), Arrays.stream(t.get(0)).boxed().toList(), "(1,2)");
    }

    private static void greedyTrap() {
        // 经典反例："alpha alpha beta" 序列 alpha@1 alpha@2 beta@3
        // 若每次贪心选“最早的 alpha”(=1)，"alpha alpha beta" slop0 会误判不存在；
        // 穷举应得到 2 种命中：(1,2,3) 与 —— 注意这里只有一个 beta@3，
        // 所以合法元组只有 (1,2,3)。陷阱体现在只用单个最早候选的算法会漏掉后续组合，
        // 这里用 "alpha beta" + 更长重复序列验证多解：见 aba 用例。
        InvertedIndex idx = index("alpha alpha beta");
        List<int[]> t = tuples(find(idx, "alpha alpha beta", 0));
        Asserts.assertEquals(1, t.size(), "alpha alpha beta slop0 matches once");
        Asserts.assertEquals(List.of(1, 2, 3), Arrays.stream(t.get(0)).boxed().toList(),
                "(1,2,3)");

        // 真正的贪心陷阱：序列 x x y（位置 1,2,3），查询 "x y" slop1。
        // 贪心“为 x 选最早位置 1”仍可命中 (1,3)，但会漏掉以第二个 x 开头的 (2,3)。
        // 我们要求返回全部组合（2 个），证明没有贪心丢解。
        InvertedIndex trap = index("x x y");
        List<int[]> xy = tuples(find(trap, "x y", 1));
        Asserts.assertEquals(2, xy.size(), "both x occurrences can start the phrase");
        Asserts.assertEquals(List.of(1, 3), Arrays.stream(xy.get(0)).boxed().toList(), "(1,3)");
        Asserts.assertEquals(List.of(2, 3), Arrays.stream(xy.get(1)).boxed().toList(), "(2,3)");

        // slop0 时只有 (2,3) 合法 —— 贪心地固定在位置1反而会错误地报告“无匹配”
        List<int[]> xy0 = tuples(find(trap, "x y", 0));
        Asserts.assertEquals(1, xy0.size(), "slop0 only adjacent pair survives");
        Asserts.assertEquals(List.of(2, 3), Arrays.stream(xy0.get(0)).boxed().toList(), "(2,3)");
    }

    private static void abaEnumeration() {
        // 序列 a b a（位置 1,2,3），查询 "a b a"：
        //   slop0: (1,2,3) 1 种
        //   slop1: 同上（无更多候选），1 种
        InvertedIndex idx = index("a b a");
        List<int[]> s0 = tuples(find(idx, "a b a", 0));
        Asserts.assertEquals(1, s0.size(), "slop0 one tuple");
        Asserts.assertEquals(List.of(1, 2, 3), Arrays.stream(s0.get(0)).boxed().toList(),
                "(1,2,3)");
        List<int[]> s1 = tuples(find(idx, "a b a", 1));
        Asserts.assertEquals(1, s1.size(), "slop1 still one tuple");

        // 更长序列 a b a b a，查询 "a b a"：
        //   slop0 精确三gram：(1,2,3)、(3,4,5)
        //   slop1（相邻差<=2）：仍只有 (1,2,3)、(3,4,5) ——
        //     如 (1,2,5) 末段差3、(1,4,5) 首段差3 均不合法
        InvertedIndex idx2 = index("a b a b a");
        List<int[]> e0 = tuples(find(idx2, "a b a", 0));
        Asserts.assertEquals(2, e0.size(), "slop0 two exact trigrams");
        Asserts.assertContains(e0, new int[] {1, 2, 3}, "contains (1,2,3)");
        Asserts.assertContains(e0, new int[] {3, 4, 5}, "contains (3,4,5)");

        List<int[]> e1 = tuples(find(idx2, "a b a", 1));
        Asserts.assertEquals(2, e1.size(), "slop1 still two tuples (gaps of 3 not allowed)");
        Asserts.assertContains(e1, new int[] {1, 2, 3}, "123");
        Asserts.assertContains(e1, new int[] {3, 4, 5}, "345");

        // slop2（相邻差<=3）放开：(1,2,3)(1,2,5)(1,4,5)(3,4,5) 共 4 种
        List<int[]> e2 = tuples(find(idx2, "a b a", 2));
        Asserts.assertEquals(4, e2.size(), "slop2 four tuples (exhaustive)");
        Asserts.assertContains(e2, new int[] {1, 2, 3}, "123");
        Asserts.assertContains(e2, new int[] {1, 2, 5}, "125");
        Asserts.assertContains(e2, new int[] {1, 4, 5}, "145");
        Asserts.assertContains(e2, new int[] {3, 4, 5}, "345");

        // 与蛮力参考实现逐位一致
        PhraseQuery q1 = PhraseQuery.of(List.of("a", "b", "a"), 2);
        List<int[]> brute = BruteForceReference.enumerate(idx2, "x", q1);
        Asserts.assertEquals(4, brute.size(), "brute force count");
        for (int i = 0; i < e2.size(); i++) {
            Asserts.assertEquals(Arrays.stream(e2.get(i)).boxed().toList(), brute.get(i),
                    "tuple " + i + " matches brute force");
        }
    }

    private static void limit() {
        InvertedIndex idx = index("a a a a b b b b b");
        // a@1..4, b@5..9，"a b" slop1（差<=2）：(3,5) (4,5) (4,6) 共 3 种
        List<int[]> full = tuples(new PhraseMatcher(100).find(idx, "x",
                PhraseQuery.of(List.of("a", "b"), 1)));
        Asserts.assertEquals(3, full.size(), "baseline three matches");
        List<int[]> capped = tuples(new PhraseMatcher(1).find(idx, "x",
                PhraseQuery.of(List.of("a", "b"), 1)));
        Asserts.assertEquals(1, capped.size(), "limit caps output to 1");
    }

    private static void missingTerm() {
        InvertedIndex idx = index("a b");
        Asserts.assertEquals(0, find(idx, "a c", 0).size(), "missing term c -> no match");
        Asserts.assertEquals(0, find(idx, "a c", 10).size(), "missing term at slop10");
    }
}
