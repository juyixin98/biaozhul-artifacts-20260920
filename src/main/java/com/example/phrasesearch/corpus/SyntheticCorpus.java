package com.example.phrasesearch.corpus;

import com.example.phrasesearch.model.Document;

import java.util.LinkedHashMap;
import java.util.List;

/**
 * 自建合成语料（确定性、小型、可手工穷举）。
 *
 * 设计目标——每条语料都服务于某个要被锁定的语义：
 * <ul>
 *   <li>doc1: 普通连续精确短语 "quick brown fox"。</li>
 *   <li>doc2: 跨字段短语：title 结尾的 "phrase" 紧接 body 开头的 "search"。</li>
 *   <li>doc3: 【重复词】连续重复 "that that"；以及非连续 "that ... that"。</li>
 *   <li>doc4: 重复词 + 干扰：三个 had，验证 "had had" 的相邻重复只产生 2 种命中，
 *       且 slop 枚举不会把同一位置复用两次。</li>
 *   <li>doc5: 【跨字段 + 停用词】title 与 body 边界处隔着停用词 the；
 *       默认分析器下 "system the quick" 精确命中，"system quick" 需要 slop=1；
 *       stop_gap 分析器下 the 留下位置洞，行为不同（由测试锁定）。</li>
 *   <li>doc6: 短语出现在两种距离上（相邻 + 有间隙），用于 slop 边界穷举。</li>
 *   <li>doc7: 同一短语跨字段边界“差一个词”的两种形态，配合 doc2 验证字段顺序。</li>
 *   <li>doc8: 完全不含目标词的干扰文档。</li>
 * </ul>
 */
public final class SyntheticCorpus {

    private SyntheticCorpus() {
    }

    public static List<Document> documents() {
        return List.of(
                doc("doc1", fields(
                        "title", "Quick Brown Fox Notes",
                        "body", "the quick brown fox jumps over the lazy dog")),
                doc("doc2", fields(
                        "title", "Phrase Search Intro",
                        "body", "search engines build an inverted index first")),
                doc("doc3", fields(
                        "title", "That That Discussion",
                        "body", "I think that that example is wrong and that this one is right")),
                doc("doc4", fields(
                        "title", "Had Had Had",
                        "body", "she said that he had had enough and had left")),
                doc("doc5", fields(
                        "title", "Index System",
                        "body", "the quick brown index stores positions")),
                doc("doc6", fields(
                        "title", "Distance Test",
                        "body", "alpha beta alpha gamma beta alpha delta alpha")),
                doc("doc7", fields(
                        "title", "Search Engine Notes",
                        "body", "engine tuning requires careful phrase queries")),
                doc("doc8", fields(
                        "title", "Unrelated Content",
                        "body", "music festivals and coffee shops dominate this text"))
        );
    }

    /** 显式按 name,value 出现顺序构造字段（跨字段拼接顺序依赖它，不能用无序的 Map.of）。 */
    private static LinkedHashMap<String, String> fields(String... kv) {
        if (kv.length % 2 != 0) {
            throw new IllegalArgumentException("fields() needs name/value pairs");
        }
        LinkedHashMap<String, String> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            m.put(kv[i], kv[i + 1]);
        }
        return m;
    }

    private static Document doc(String id, LinkedHashMap<String, String> fields) {
        return new Document(id, fields);
    }
}
