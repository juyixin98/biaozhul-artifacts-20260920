package phrase.test;

import phrase.search.DocMatch;
import phrase.search.Match;
import phrase.search.SearchResult;
import phrase.search.SearchService;

import java.util.List;

/**
 * 在合成语料上的端到端检索测试，对应任务验收点：
 * 重复词不可重用同一位置、跨字段短语、字段限制、fieldGap、停用词保留位置。
 */
public final class SearcherTest {

    public static void register(TestRunner runner) {
        runner.add("search/d1-repeated-echo-enumerates-all-pairs",
                SearcherTest::d1RepeatedEcho);
        runner.add("search/d2-cross-field-phrase-data-pipeline",
                SearcherTest::d2CrossField);
        runner.add("search/cross-field-blocked-by-field-gap",
                SearcherTest::fieldGapBlocksCrossField);
        runner.add("search/d4-alpha-alpha-beta-greedy-trap-on-corpus",
                SearcherTest::d4GreedyTrap);
        runner.add("search/field-scope-restricts-matches", SearcherTest::fieldScope);
        runner.add("search/stopword-keeps-position-cat-mat-needs-slop",
                SearcherTest::stopwordPositions);
        runner.add("search/stopword-query-with-stopword-in-it",
                SearcherTest::stopwordQuery);
        runner.add("search/query-only-stopwords-rejected", SearcherTest::onlyStopwords);
        runner.add("search/validation-errors", SearcherTest::validation);
        runner.add("search/analyzer-independent-indexes", SearcherTest::twoIndexes);
    }

    private static DocMatch doc(SearchResult r, String docId) {
        return r.hits().stream().filter(h -> h.docId().equals(docId)).findFirst()
                .orElse(null);
    }

    private static List<List<Integer>> positions(SearchResult r, String docId) {
        DocMatch dm = doc(r, docId);
        Asserts.assertTrue(dm != null, "expected hit in " + docId);
        return dm.matches().stream().map(Match::positions).toList();
    }

    private static void d1RepeatedEcho() {
        SearchService svc = new SearchService(0, 100);
        // d1 title="echo chamber"：echo@1；body="we echo echo echo echo today"：
        // we@3 echo@4,5,6,7 today@8（fieldGap=0，字段首尾紧邻）
        // "echo echo" slop0：body 内 3 个相邻对 (4,5)(5,6)(6,7)
        SearchResult r = svc.search("standard", "echo echo", null, 0, null);
        Asserts.assertEquals(3, doc(r, "d1").matchCount(), "3 adjacent echo pairs in body");
        List<List<Integer>> ps = positions(r, "d1");
        Asserts.assertTrue(ps.contains(List.of(4, 5)), "(4,5)");
        Asserts.assertTrue(ps.contains(List.of(5, 6)), "(5,6)");
        Asserts.assertTrue(ps.contains(List.of(6, 7)), "(6,7)");
        for (Match m : doc(r, "d1").matches()) {
            Asserts.assertFalse(m.crossField(), "all within body");
        }

        // slop1：额外允许差 2，跨字段 title.echo@1 -> body.echo@4 差3，
        // 以及 body 内 (4,6)(5,7) —— 但跨字段那对差3仍不允许，故新增 2 条，共 5 条
        SearchResult r1 = svc.search("standard", "echo echo", null, 1, null);
        Asserts.assertEquals(5, doc(r1, "d1").matchCount(),
                "slop1 adds (4,6),(5,7); cross-field distance 3 still excluded");
        Asserts.assertFalse(doc(r1, "d1").matches().stream().anyMatch(Match::crossField),
                "no cross-field match yet (distance 3 > slop1)");

        // slop3：title.echo@1 -> body.echo@4 成为跨字段命中
        SearchResult r3 = svc.search("standard", "echo echo", null, 3, null);
        Match cross = doc(r3, "d1").matches().stream()
                .filter(Match::crossField).findFirst().orElse(null);
        Asserts.assertTrue(cross != null, "slop3 produces a cross-field (1,4) match");
        Asserts.assertEquals(List.of(1, 4), cross.positions(), "title echo@1 -> body echo@4");
        Asserts.assertEquals(List.of("title", "body"), cross.fields(), "fields recorded");

        // d3: title="rain watch" rain@1；body="the rain brings more rain"
        // rain@4(=3+局部2) 与 rain@7(=3+5)；slop0 无 rain 对；
        // slop2（差<=3）：(1,4) 跨字段 与 (4,7) body 内 共 2 对
        SearchResult rain = svc.search("standard", "rain rain", null, 0, null);
        Asserts.assertTrue(doc(rain, "d3") == null, "slop0 no rain rain (gaps too large)");
        SearchResult rain2 = svc.search("standard", "rain rain", null, 2, null);
        Asserts.assertEquals(2, doc(rain2, "d3").matchCount(),
                "slop2: (1,4) cross-field and (4,7) within body");
        List<List<Integer>> rainPos = positions(rain2, "d3");
        Asserts.assertTrue(rainPos.contains(List.of(1, 4)), "(1,4) cross-field");
        Asserts.assertTrue(rainPos.contains(List.of(4, 7)), "(4,7) same field");
        Asserts.assertTrue(doc(rain2, "d3").matches().stream()
                        .filter(m -> m.positions().equals(List.of(1, 4)))
                        .findFirst().orElseThrow().crossField(),
                "(1,4) marked cross field");
    }

    private static void d2CrossField() {
        SearchService svc = new SearchService(0, 100);
        // d2: title="infra notes"(全局1..2), body="we build a pipeline for data"(3..8),
        // tags="pipeline streaming"(9..10)；body pipeline 全局6，data 全局8，tags pipeline@9
        // "data pipeline" slop0：8->9 跨 body/tags 字段，1 条跨字段命中
        SearchResult r = svc.search("standard", "data pipeline", null, 0, null);
        DocMatch d2 = doc(r, "d2");
        Asserts.assertTrue(d2 != null, "d2 matches data pipeline across fields");
        Asserts.assertEquals(1, d2.matchCount(), "exactly one cross-field occurrence");
        Match m = d2.matches().get(0);
        Asserts.assertEquals(List.of(8, 9), m.positions(), "data@8 pipeline@9");
        Asserts.assertEquals(List.of("body", "tags"), m.fields(), "field tags attached");
        Asserts.assertTrue(m.crossField(), "flagged cross-field");

        // 反向 "pipeline data" slop0：pipeline@6 -> data@8 差2，slop0 不命中，slop1 命中
        Asserts.assertTrue(doc(svc.search("standard", "pipeline data", null, 0, null), "d2") == null,
                "pipeline@6 data@8 gap of 1 -> slop0 no match");
        SearchResult rev = svc.search("standard", "pipeline data", null, 1, null);
        Match rm = doc(rev, "d2").matches().get(0);
        Asserts.assertEquals(List.of(6, 8), rm.positions(), "pipeline@6 data@8 inside body");
        Asserts.assertFalse(rm.crossField(), "not cross-field");
    }

    private static void fieldGapBlocksCrossField() {
        // fieldGap=1：字段间插入 1 个虚拟位置
        // d7: title="deep blue"(blue@2), body="ocean currents"(ocean@4)
        // "blue ocean"：位置差 2 -> slop0/1 语义：slop0 不命中，slop1 命中
        SearchService g1 = new SearchService(1, 100);
        SearchResult r0 = g1.search("standard", "blue ocean", null, 0, null);
        Asserts.assertTrue(doc(r0, "d7") == null, "gap1: slop0 cannot cross field boundary");
        SearchResult r1 = g1.search("standard", "blue ocean", null, 1, null);
        Asserts.assertEquals(1, doc(r1, "d7").matchCount(),
                "gap1: slop1 crosses the single virtual gap");
        Asserts.assertEquals(List.of(2, 4), doc(r1, "d7").matches().get(0).positions(),
                "(2,4) with virtual position 3");

        // fieldGap=2：需要 slop>=2
        SearchService g2 = new SearchService(2, 100);
        Asserts.assertTrue(doc(g2.search("standard", "blue ocean", null, 1, null), "d7") == null,
                "gap2 needs slop2");
        Asserts.assertEquals(1,
                doc(g2.search("standard", "blue ocean", null, 2, null), "d7").matchCount(),
                "gap2 slop2 ok");

        // gap=0 时 slop0 直接命中（验收默认行为）
        SearchService g0 = new SearchService(0, 100);
        Asserts.assertEquals(1,
                doc(g0.search("standard", "blue ocean", null, 0, null), "d7").matchCount(),
                "gap0 slop0 adjacent across boundary");
    }

    private static void d4GreedyTrap() {
        SearchService svc = new SearchService(0, 100);
        // d4 title="pattern"@1，body alpha@2 alpha@3 beta@4
        SearchResult r = svc.search("standard", "alpha alpha beta", null, 0, null);
        DocMatch d4 = doc(r, "d4");
        Asserts.assertEquals(1, d4.matchCount(), "(2,3,4) exact");
        Asserts.assertEquals(List.of(2, 3, 4), d4.matches().get(0).positions(), "positions");

        // terms 数组直传也等价
        SearchResult viaTerms = svc.search("standard", null,
                List.of("Alpha", "ALPHA", "beta"), 0, null);
        Asserts.assertEquals(1, doc(viaTerms, "d4").matchCount(),
                "terms normalized to lowercase");
    }

    private static void fieldScope() {
        SearchService svc = new SearchService(0, 100);
        // 全字段：d2 "data pipeline" 跨字段命中
        Asserts.assertEquals(1, doc(svc.search("standard", "data pipeline", null, 0, null),
                "d2").matchCount(), "all-fields hit");
        // 限制 body：data@body8 -> pipeline 不在 body 其后 -> 无命中
        SearchResult bodyOnly = svc.search("standard", "data pipeline", null, 0, "body");
        Asserts.assertTrue(doc(bodyOnly, "d2") == null,
                "field=body excludes the tags pipeline");
        // 限制 tags：data 不在 tags -> 无命中
        SearchResult tagsOnly = svc.search("standard", "data pipeline", null, 0, "tags");
        Asserts.assertTrue(doc(tagsOnly, "d2") == null, "field=tags excludes body data");

        // 单字段内重复词：d1 echo echo 限定 body 仍是 3
        SearchResult echoBody = svc.search("standard", "echo echo", null, 0, "body");
        Asserts.assertEquals(3, doc(echoBody, "d1").matchCount(),
                "body-scoped repeated echo pairs");
        SearchResult echoTitle = svc.search("standard", "echo echo", null, 0, "title");
        Asserts.assertTrue(doc(echoTitle, "d1") == null, "title has only one echo");
    }

    private static void stopwordPositions() {
        SearchService svc = new SearchService(0, 100);
        // d5: title "the cat" -> cat@2；body "the cat sat in the mat" -> cat@4 sat@5 mat@8
        // （the/in 被删除但位置保留：body 局部位置 cat@2 sat@3 mat@6，全局 +2 偏移）
        // "cat mat"：body 内 cat@4 -> mat@8 差 4 -> slop0..2 不命中，slop3 命中
        for (int slop = 0; slop <= 2; slop++) {
            SearchResult r = svc.search("stopword", "cat mat", null, slop, null);
            Asserts.assertTrue(doc(r, "d5") == null,
                    "cat mat needs slop>=3 because removed stopwords keep positions; slop="
                            + slop);
        }
        SearchResult hit = svc.search("stopword", "cat mat", null, 3, null);
        Match m = doc(hit, "d5").matches().get(0);
        Asserts.assertEquals(List.of(4, 8), m.positions(),
                "cat@4 mat@8 in stopword index (positions 5..7 were sat,in,the)");

        // 查询串里带停用词："cat the mat" 分析后为 cat,mat —— 行为与上面一致，
        // 停用词在查询侧同样被删除（不占查询词位），位置差仍由索引空位体现。
        SearchResult qws = svc.search("stopword", "cat the mat", null, 3, null);
        Asserts.assertEquals(List.of("cat", "mat"), qws.queryTerms(),
                "stopword removed from query terms");
        Asserts.assertEquals(1, doc(qws, "d5").matchCount(), "same match as cat mat");

        // "cat sat mat" slop2（相邻差<=3）穷举两条：
        //   (2,5,8) title.cat -> body.sat -> body.mat（跨字段）
        //   (4,5,8) body 内部 cat@4 sat@5 mat@8
        SearchResult csm = svc.search("stopword", "cat sat mat", null, 2, null);
        DocMatch d5csm = doc(csm, "d5");
        Asserts.assertEquals(2, d5csm.matchCount(), "two exhaustive tuples at slop2");
        List<List<Integer>> csmPos = d5csm.matches().stream().map(Match::positions).toList();
        Asserts.assertTrue(csmPos.contains(List.of(2, 5, 8)), "(2,5,8) cross-field");
        Asserts.assertTrue(csmPos.contains(List.of(4, 5, 8)), "(4,5,8) within body");
        Match inBody = d5csm.matches().stream()
                .filter(x -> x.positions().equals(List.of(4, 5, 8))).findFirst().orElseThrow();
        Asserts.assertEquals(List.of("body", "body", "body"), inBody.fields(), "all body");
        Asserts.assertFalse(inBody.crossField(), "(4,5,8) not cross-field");
        Asserts.assertTrue(doc(svc.search("stopword", "cat sat mat", null, 1, null), "d5") == null,
                "slop1 not enough (both tuples contain a gap of 3: in,the occupy positions)");
    }

    private static void stopwordQuery() {
        SearchService svc = new SearchService(0, 100);
        // 停用词模式下 "the cat" -> [cat]：单词查询，d5 标题与正文共 2 个 cat
        SearchResult r = svc.search("stopword", "the cat", null, 0, null);
        Asserts.assertEquals(List.of("cat"), r.queryTerms(), "the removed, cat kept");
        DocMatch d5 = doc(r, "d5");
        Asserts.assertEquals(2, d5.matchCount(), "cat appears in title and body");
        Asserts.assertEquals(List.of(2), d5.matches().get(0).positions(), "title cat@2");
        Asserts.assertEquals(List.of(4), d5.matches().get(1).positions(), "body cat@4");
    }

    private static void onlyStopwords() {
        SearchService svc = new SearchService(0, 100);
        Asserts.assertThrows(IllegalArgumentException.class,
                () -> svc.search("stopword", "the a an", null, 0, null),
                "query that removes to zero terms rejected");
        Asserts.assertThrows(IllegalArgumentException.class,
                () -> svc.search("standard", "  ", null, 0, null), "blank query rejected");
        Asserts.assertThrows(IllegalArgumentException.class,
                () -> svc.search("standard", null, List.of(), 0, null),
                "empty terms rejected");
        Asserts.assertThrows(IllegalArgumentException.class,
                () -> svc.search("standard", "x", null, -1, null), "negative slop rejected");
        Asserts.assertThrows(IllegalArgumentException.class,
                () -> svc.search("bogus", "x", null, 0, null), "bad analyzer rejected");
        Asserts.assertThrows(IllegalArgumentException.class,
                () -> svc.search("standard", "x", null, 0, "nope"), "bad field rejected");
    }

    private static void validation() {
        SearchService svc = new SearchService(0, 100);
        Asserts.assertThrows(IllegalArgumentException.class,
                () -> svc.search("standard", null, List.of("ok", "bad term"), 0, null),
                "terms with spaces rejected");
        SearchResult r = svc.search("standard", "ECHO, ECHO!", null, 0, null);
        Asserts.assertEquals(List.of("echo", "echo"), r.queryTerms(),
                "query string tokenized and lowercased");
    }

    private static void twoIndexes() {
        // standard 索引保留 the；stopword 索引删除 the
        SearchService svc = new SearchService(0, 100);
        SearchResult std = svc.search("standard", "the cat", null, 0, null);
        // standard: d5 title the@1 cat@2 精确相邻 -> 命中
        Asserts.assertEquals(List.of(1, 2),
                doc(std, "d5").matches().get(0).positions(),
                "standard index keeps the@1 (title exact phrase)");
        SearchResult sw = svc.search("stopword", "the cat", null, 0, null);
        Asserts.assertEquals(List.of("cat"), sw.queryTerms(),
                "stopword index drops the from query");
    }
}
