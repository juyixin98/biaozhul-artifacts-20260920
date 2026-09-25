package com.example.phrasesearch.tests;

import com.example.phrasesearch.analyze.StandardAnalyzer;
import com.example.phrasesearch.corpus.SyntheticCorpus;
import com.example.phrasesearch.index.Index;
import com.example.phrasesearch.model.Document;
import com.example.phrasesearch.model.Posting;
import com.example.phrasesearch.query.PhraseMatch;
import com.example.phrasesearch.query.PhraseQuery;
import com.example.phrasesearch.search.Searcher;

import java.util.ArrayList;
import java.util.List;

/**
 * 检索语义测试。语料（standard 分析器，全局位置流）在每个用例的注释中穷举列出。
 */
final class SearchTest {

    private SearchTest() {
    }

    private static Index index;
    private static Searcher searcher;

    static void run() {
        index = new Index(new StandardAnalyzer());
        for (Document d : SyntheticCorpus.documents()) {
            index.addDocument(d);
        }
        searcher = new Searcher();

        exactPhraseTwoOccurrences();
        exactPhraseRequiresAdjacency();
        slopBoundaryEnumeration();
        repeatedTermThatThat();
        tripleRepeatHadHad();
        repeatedAlphaAlphaOverSlop();
        crossFieldPhraseAtBoundary();
        crossFieldNeedsSlopForInterveningWord();
        crossFieldStopwordDoc5();
        fieldScopedSearch();
        singleTermQueryReturnsEveryPosition();
        noMatchDocument();
        matchOffsetsAreCorrect();
        everyMatchUsesDistinctPositions();
        slopZeroMatchesAreSubsetOfLargerSlop();
    }

    // ---------- 辅助 ----------

    private static PhraseQuery q(String phrase, int slop) {
        List<String> terms = new ArrayList<>();
        for (String t : phrase.split(" ")) {
            terms.add(t);
        }
        return new PhraseQuery(terms, slop, null, true, phrase);
    }

    private static List<PhraseMatch> search(String phrase, int slop) {
        return searcher.search(index, q(phrase, slop));
    }

    private static List<PhraseMatch> inDoc(String docId, List<PhraseMatch> all) {
        return all.stream().filter(m -> m.docExternalId().equals(docId)).toList();
    }

    private static List<Integer> spans(List<PhraseMatch> ms) {
        List<Integer> out = new ArrayList<>();
        for (PhraseMatch m : ms) {
            out.add(m.startPosition());
            out.add(m.endPosition());
        }
        return out;
    }

    // ---------- 用例 ----------

    /**
     * doc1 全局位置：
     * title: quick0 brown1 fox2 notes3
     * body : the4 quick5 brown6 fox7 jumps8 over9 the10 lazy11 dog12
     */
    private static void exactPhraseTwoOccurrences() {
        List<PhraseMatch> all = search("quick brown fox", 0);
        List<PhraseMatch> d1 = inDoc("doc1", all);
        Assert.equals("精确短语: 只有 doc1 命中", 1L,
                all.stream().map(PhraseMatch::docExternalId).distinct().count());
        Assert.equals("精确短语: doc1 内 2 次出现（title+body）", 2L, d1.size());
        Assert.equals("精确短语: 两次跨度 (0,2) 与 (5,7)",
                List.of(0, 2, 5, 7), spans(d1));
        Assert.equals("精确短语: slopUsed 均为 0", List.of(0),
                d1.stream().map(PhraseMatch::slopUsed).distinct().toList());
    }

    private static void exactPhraseRequiresAdjacency() {
        // doc6 body: alpha2 beta3 alpha4 gamma5 beta6 ...
        // "beta gamma"：beta3 与 gamma5 之间隔着 alpha4，slop=0 不命中，slop=1 命中。
        List<PhraseMatch> m0 = inDoc("doc6", search("beta gamma", 0));
        Assert.equals("非相邻短语 slop=0 不命中", 0, m0.size());
        List<PhraseMatch> m1 = inDoc("doc6", search("beta gamma", 1));
        Assert.equals("slop=1 命中 beta3 gamma5", List.of(3, 5), spans(m1));
    }

    /**
     * doc6 body 中 alpha 的位置: {2,4,7,9}（title distance0 test1 之后）。
     * 两两组合（a<b）的 slopUsed = b-a-1:
     *   (2,4)=1 (2,7)=4 (2,9)=6
     *   (4,7)=2 (4,9)=4
     *   (7,9)=1
     * 共 6 对，据此穷举各 slop 阈值的命中数。
     */
    private static void slopBoundaryEnumeration() {
        int[] counts = new int[7];
        for (int slop = 0; slop <= 6; slop++) {
            counts[slop] = inDoc("doc6", search("alpha alpha", slop)).size();
        }
        Assert.equals("slop 边界穷举 alpha alpha: [0,2,3,3,5,5,6]",
                List.of(0, 2, 3, 3, 5, 5, 6),
                java.util.Arrays.stream(counts).boxed().toList());
    }

    /**
     * doc3 中 that 的位置: {0,1,5,6,11}
     *   title: That0 That1 Discussion2
     *   body : I3 think4 that5 that6 example7 is8 wrong9 and10 that11 this12 ...
     */
    private static void repeatedTermThatThat() {
        List<PhraseMatch> m0 = inDoc("doc3", search("that that", 0));
        Assert.equals("that that slop=0: (0,1) 与 (5,6)",
                List.of(0, 1, 5, 6), spans(m0));

        // slop=5 时 8 对（见类注释穷举）：
        // (0,1)=0 (0,5)=4 (0,6)=5 (1,5)=3 (1,6)=4 (5,6)=0 (5,11)=5 (6,11)=4
        List<PhraseMatch> m5 = inDoc("doc3", search("that that", 5));
        Assert.equals("that that slop=5: 8 对", 8, m5.size());
        Assert.equals("that that slop=5: 跨度集合",
                List.of(0, 1, 0, 5, 0, 6, 1, 5, 1, 6, 5, 6, 5, 11, 6, 11),
                spans(m5));
        Assert.equals("that that slop=6 仍是 8 对（(0,11) 需要 10）",
                8, inDoc("doc3", search("that that", 6)).size());
        Assert.equals("that that slop=10 包含全部 10 对",
                10, inDoc("doc3", search("that that", 10)).size());
    }

    /**
     * doc4: title had0 had1 had2; body ... had7 had8 ... had11
     * 三个连续 had 必须产生 2 个相邻 "had had"，而不是 3 个（位置不能复用为同一个词项）。
     */
    private static void tripleRepeatHadHad() {
        List<PhraseMatch> m0 = inDoc("doc4", search("had had", 0));
        Assert.equals("三连 had: slop=0 命中 (0,1)(1,2)(7,8) 共 3 次",
                List.of(0, 1, 1, 2, 7, 8), spans(m0));
        // 每次命中里的两个位置必须不同（严格递增）
        Assert.isTrue("三连 had: 无位置复用",
                m0.stream().allMatch(m -> m.postings().get(0).position()
                        < m.postings().get(1).position()));
        List<PhraseMatch> m1 = inDoc("doc4", search("had had", 1));
        Assert.equals("三连 had: slop=1 增加 (0,2)，共 4 次", 4, m1.size());
    }

    private static void repeatedAlphaAlphaOverSlop() {
        // 已在 slopBoundaryEnumeration 穷举，这里再锁定“绝不存在同位置命中”
        List<PhraseMatch> m = search("alpha alpha", 6);
        Assert.isTrue("alpha alpha: 所有命中的两个位置互不相同",
                m.stream().allMatch(x -> x.postings().get(0).position()
                        != x.postings().get(1).position()));
    }

    /**
     * doc2: title phrase0 search1 intro2; body search3 engines4 ...
     * "phrase search" 精确命中 title 的 (0,1)；跨字段的 (0,3) slopUsed=2。
     */
    private static void crossFieldPhraseAtBoundary() {
        List<PhraseMatch> m0 = inDoc("doc2", search("phrase search", 0));
        Assert.equals("跨字段: slop=0 只有 title 内 (0,1)", List.of(0, 1), spans(m0));
        Assert.isTrue("title 内命中 crossField=false",
                m0.stream().allMatch(m -> !m.crossField()));

        List<PhraseMatch> m2 = inDoc("doc2", search("phrase search", 2));
        Assert.equals("跨字段: slop=2 增加边界处 (0,3)", 2, m2.size());
        PhraseMatch cross = m2.stream().filter(PhraseMatch::crossField).findFirst().orElseThrow();
        Assert.equals("跨字段命中: 首词在 title", "title", cross.postings().get(0).field());
        Assert.equals("跨字段命中: 末词在 body", "body", cross.postings().get(1).field());
        Assert.equals("跨字段命中: slopUsed=2", 2, cross.slopUsed());
    }

    /**
     * doc7: title search0 engine1 notes2; body engine3 ...
     * "search engine" slop0=(0,1)；跨字段 (0,3) 需要 slop=2。slop=1 不应越界命中。
     */
    private static void crossFieldNeedsSlopForInterveningWord() {
        Assert.equals("doc7 slop=0: 1 次", 1, inDoc("doc7", search("search engine", 0)).size());
        Assert.equals("doc7 slop=1: 仍 1 次（title 与 body 间隔着 notes2）",
                1, inDoc("doc7", search("search engine", 1)).size());
        Assert.equals("doc7 slop=2: 跨字段命中出现，共 2 次",
                2, inDoc("doc7", search("search engine", 2)).size());
    }

    /**
     * doc5: title index0 system1; body the2 quick3 brown4 index5 stores6 positions7
     * 默认分析器【停用词保留位置】："system the quick" 在 slop=0 跨字段精确命中。
     */
    private static void crossFieldStopwordDoc5() {
        List<PhraseMatch> exact = inDoc("doc5", search("system the quick", 0));
        Assert.equals("停用词保留: system the quick slop=0 跨字段精确命中 (1,2,3)",
                List.of(1, 3),
                exact.isEmpty() ? List.of() : List.of(exact.get(0).startPosition(),
                        exact.get(0).endPosition()));
        Assert.isTrue("该命中跨越 title/body",
                exact.stream().allMatch(PhraseMatch::crossField));

        Assert.equals("system quick slop=0 不命中",
                0, inDoc("doc5", search("system quick", 0)).size());
        List<PhraseMatch> sq1 = inDoc("doc5", search("system quick", 1));
        Assert.equals("system quick slop=1 跨字段命中 (1,3)",
                List.of(1, 3), spans(sq1));

        // "quick brown index" 在 body 内位置为 3,4,5，完全相邻，slop=0 即命中
        List<PhraseMatch> qbi = inDoc("doc5", search("quick brown index", 0));
        Assert.equals("quick brown index slop=0 命中 (3,4,5)",
                List.of(3, 5), qbi.size() == 1
                        ? List.of(qbi.get(0).startPosition(), qbi.get(0).endPosition())
                        : List.of(-1, -1));
    }

    private static void fieldScopedSearch() {
        // 限定 body：phrase 只在 doc2 title -> 不命中
        PhraseQuery bodyOnly = new PhraseQuery(List.of("phrase", "search"), 2, "body",
                true, "phrase search");
        List<PhraseMatch> bodyHits = searcher.search(index, bodyOnly);
        Assert.equals("字段限定 body: phrase search 不命中", 0, bodyHits.size());

        // 限定 title：跨字段的 (0,3) 因末词在 body 被过滤，只剩 (0,1)
        PhraseQuery titleOnly = new PhraseQuery(List.of("phrase", "search"), 2, "title",
                true, "phrase search");
        List<PhraseMatch> titleHits = searcher.search(index, titleOnly);
        Assert.equals("字段限定 title: 仅 1 次（跨字段命中被排除）", 1, titleHits.size());
        Assert.isTrue("字段限定: 命中位置都在 title",
                titleHits.stream().flatMap(m -> m.postings().stream())
                        .allMatch(p -> p.field().equals("title")));
    }

    private static void singleTermQueryReturnsEveryPosition() {
        List<PhraseMatch> fox = inDoc("doc1", search("fox", 0));
        Assert.equals("单词项 fox: 返回每个出现位置 2 与 7",
                List.of(2, 7), fox.stream().map(PhraseMatch::startPosition).toList());
    }

    private static void noMatchDocument() {
        Assert.equals("doc8 不含目标词",
                0, inDoc("doc8", search("quick brown", 0)).size());
        Assert.equals("不存在词项不产生命中", 0, search("zzz missing", 0).size());
    }

    private static void matchOffsetsAreCorrect() {
        List<PhraseMatch> m = inDoc("doc1", search("quick", 0));
        // body 中 quick 位于全局位置 5；原文 "the quick brown..." 的字符偏移 4..9
        Posting bodyQuick = m.stream().flatMap(x -> x.postings().stream())
                .filter(p -> p.position() == 5).findFirst().orElseThrow();
        Assert.equals("body quick startOffset=4", 4, bodyQuick.startOffset());
        Assert.equals("body quick endOffset=9", 9, bodyQuick.endOffset());
    }

    /** 不变量：任何一次命中中，所有词项的位置两两不同且严格递增。 */
    private static void everyMatchUsesDistinctPositions() {
        String[] probes = {"that that", "had had had", "alpha alpha alpha",
                "quick brown fox", "the the", "beta alpha beta"};
        for (String probe : probes) {
            for (int slop = 0; slop <= 8; slop++) {
                for (PhraseMatch m : search(probe, slop)) {
                    List<Posting> ps = m.postings();
                    for (int i = 1; i < ps.size(); i++) {
                        Assert.isTrue("位置严格递增 [" + probe + " slop=" + slop + "]",
                                ps.get(i - 1).position() < ps.get(i).position());
                    }
                    long distinct = ps.stream().map(Posting::position).distinct().count();
                    Assert.equals("位置两两不同 [" + probe + " slop=" + slop + "]",
                            (long) ps.size(), distinct);
                }
            }
        }
    }

    /** slop 单调性：slop=k 的命中集合必须是 slop=k+1 的子集。 */
    private static void slopZeroMatchesAreSubsetOfLargerSlop() {
        for (String probe : List.of("that that", "alpha beta", "quick brown fox",
                "phrase search", "had had")) {
            List<String> prev = signature(search(probe, 0));
            for (int slop = 1; slop <= 6; slop++) {
                List<String> cur = signature(search(probe, slop));
                Assert.isTrue("slop 单调性 " + probe + " @ " + slop,
                        cur.containsAll(prev));
                prev = cur;
            }
        }
    }

    private static List<String> signature(List<PhraseMatch> matches) {
        List<String> out = new ArrayList<>();
        for (PhraseMatch m : matches) {
            StringBuilder sb = new StringBuilder(m.docExternalId());
            for (Posting p : m.postings()) {
                sb.append('@').append(p.field()).append(':').append(p.position());
            }
            out.add(sb.toString());
        }
        return out;
    }
}
