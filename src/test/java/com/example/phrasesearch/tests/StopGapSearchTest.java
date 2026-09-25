package com.example.phrasesearch.tests;

import com.example.phrasesearch.analyze.StopGapAnalyzer;
import com.example.phrasesearch.index.Index;
import com.example.phrasesearch.model.Document;
import com.example.phrasesearch.model.Posting;
import com.example.phrasesearch.query.PhraseMatch;
import com.example.phrasesearch.query.PhraseQuery;
import com.example.phrasesearch.search.Searcher;

import java.util.LinkedHashMap;
import java.util.List;

/**
 * stop_gap 分析器下的位置与检索语义测试：
 * 停用词删除后位置留洞，短语匹配必须“看见”这个洞；跨字段拼接基址也必须含洞。
 */
final class StopGapSearchTest {

    private StopGapSearchTest() {
    }

    static void run() {
        deletedStopwordLeavesHoleForPhrase();
        retainedStopwordPhraseStillExact();
        crossFieldBaseIncludesGap();
        repeatedTermsUnderStopGap();
    }

    private static Index buildIndex() {
        Index index = new Index(new StopGapAnalyzer(StopGapAnalyzer.DEFAULT_STOPWORDS));
        LinkedHashMap<String, String> f = new LinkedHashMap<>();
        f.put("title", "Index System");        // index0 system1
        f.put("body", "the quick brown fox");  // the 删除留洞2; quick3 brown4 fox5
        index.addDocument(new Document("g1", f));

        LinkedHashMap<String, String> f2 = new LinkedHashMap<>();
        f2.put("title", "Quick the Fox");      // quick0 (the 洞1) fox2
        f2.put("body", "the end");             // (the 洞3) end4
        index.addDocument(new Document("g2", f2));
        return index;
    }

    private static List<PhraseMatch> run(Index index, String phrase, int slop, String field) {
        // 查询与索引同链：先过 stop_gap 分析器（停用词被删、位置语义与文档一致）
        var terms = index.analyzer().analyze("_query", phrase).tokens()
                .stream().map(com.example.phrasesearch.model.Token::term).toList();
        if (terms.isEmpty()) {
            return List.of();
        }
        return new Searcher().search(index,
                new PhraseQuery(terms, slop, field, true, phrase));
    }

    private static List<Integer> spans(List<PhraseMatch> ms) {
        // 单文档场景使用；多文档时先过滤。输出按起始位置排序以保证比较稳定。
        return ms.stream().flatMap(m -> m.postings().stream()).map(Posting::position)
                .sorted().toList();
    }

    private static void deletedStopwordLeavesHoleForPhrase() {
        Index index = buildIndex();
        // g1: title index0 system1；body 中 the 删除留洞 -> quick3 brown4 fox5
        // "quick fox" 位置 (3,5)，中间隔着被删 the 的位置洞 4
        Assert.equals("stop_gap: \"quick fox\" slop=0 不命中（位置洞可见）",
                0L, run(index, "quick fox", 0, null)
                        .stream().filter(m -> m.docExternalId().equals("g1")).count());
        List<PhraseMatch> all = run(index, "quick fox", 1, null);
        List<PhraseMatch> hit = all.stream()
                .filter(m -> m.docExternalId().equals("g1")).toList();
        Assert.equals("stop_gap: \"quick fox\" slop=1 命中 g1 (3,5)，slopUsed=1",
                List.of(3, 5), spans(hit));
        Assert.equals("stop_gap: slopUsed 如实为 1", 1, hit.get(0).slopUsed());
    }

    private static void retainedStopwordPhraseStillExact() {
        Index index = buildIndex();
        // 查询串同样过 stop_gap 分析器："system the quick" 中 the 也会被删，
        // 只剩 [system, quick]，其位置为 (1,3) -> slopUsed=1
        List<PhraseMatch> m = run(index, "system the quick", 0, null);
        Assert.equals("stop_gap: 查询中的停用词同样被删，[system,quick] slop0 不命中",
                0, m.size());
        List<PhraseMatch> m1 = run(index, "system the quick", 1, null);
        Assert.equals("stop_gap: [system,quick] slop1 命中 (1,3)",
                List.of(1, 3), spans(m1));
    }

    private static void crossFieldBaseIncludesGap() {
        Index index = buildIndex();
        // g2 title: quick0 (洞1) fox2；body: (洞3) end4
        // 跨字段 "fox end"：位置 2 和 4，中间洞 3 -> slopUsed=1
        Assert.equals("跨字段基址含洞: \"fox end\" slop0 不命中",
                0, run(index, "fox end", 0, null).size());
        List<PhraseMatch> m1 = run(index, "fox end", 1, null);
        Assert.equals("跨字段基址含洞: \"fox end\" slop1 命中 (2,4)",
                List.of(2, 4), spans(m1));
        Assert.isTrue("跨字段标志为 true", m1.get(0).crossField());
        // "quick fox"：g2 同字段内 (0,2)；g1 的 (3,5) 同在 body 但也隔着洞
        List<PhraseMatch> qf = run(index, "quick fox", 1, null);
        Assert.equals("位置洞: \"quick fox\" slop1 命中 g2(0,2) 与 g1(3,5)",
                2, qf.size());
        Assert.equals("位置洞: 跨度集合排序后为 [0,2,3,5]",
                List.of(0, 2, 3, 5), spans(qf));
    }

    private static void repeatedTermsUnderStopGap() {
        Index index = new Index(new StopGapAnalyzer(StopGapAnalyzer.DEFAULT_STOPWORDS));
        // 注意 "a" 本身在默认停用词表内，这里用非停用词 x 才能观察“隔着停用词的重复词”
        LinkedHashMap<String, String> f = new LinkedHashMap<>();
        f.put("b", "x the x the x"); // x0 洞1 x2 洞3 x4
        index.addDocument(new Document("r", f));

        List<PhraseMatch> m0 = run(index, "x x", 0, null);
        Assert.equals("重复词隔停用词: slop0 不命中", 0, m0.size());
        // x 位于 0,2,4；各对 slopUsed：(0,2)=1、(2,4)=1、(0,4)=3
        List<PhraseMatch> m2 = run(index, "x x", 2, null);
        Assert.equals("重复词隔停用词: slop2 命中两对 (0,2)(2,4)，(0,4) 需 slop3",
                List.of(0, 2, 2, 4), spans(m2));
        Assert.equals("重复词隔停用词: 恰好 2 对", 2, m2.size());
        List<PhraseMatch> m3 = run(index, "x x", 3, null);
        Assert.equals("重复词隔停用词: slop3 三对全部命中", 3, m3.size());
    }
}
