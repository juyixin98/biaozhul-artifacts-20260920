package com.bm25pager.corpus;

import com.bm25pager.model.Document;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 自建合成语料（不读取任何外部数据源，启动时由程序生成）。
 *
 * 语料完全确定性：相同代码永远生成相同文档集合，便于自动化测试复现。
 * 设计上刻意覆盖验收要求的边界情形：
 *
 * - empty-001            空正文（""）
 * - empty-002            仅空白与标点（分词后为空）
 * - tie-001 / tie-002    针对查询 "banana" 的同分构造（长度相同、词频相同）
 * - repeat-001           同一词项大量重复（验证词频饱和而非线性放大）
 * - common-0001..        60 篇均含 "data"（验证大结果集下连续翻页不漏不重）
 * - numeric-1            数字与字母混排（如 v2、token10）
 */
public final class SyntheticCorpus {

    private SyntheticCorpus() {
    }

    public static List<Document> create() {
        List<Document> docs = new ArrayList<>();

        // ---- 明确的手工边界文档 -------------------------------------------------

        docs.add(doc("empty-001", "", Map.of("title", "Empty document")));
        docs.add(doc("empty-002", "   \t\n  ... !!!  ", Map.of("title", "Whitespace only")));

        // 两篇长度相同、banana 词频相同、其它词也一致 => banana 同分，按 docId tie-001 在前
        docs.add(doc("tie-001",
                "banana banana alpha gamma banana",
                Map.of("title", "Tie case A")));
        docs.add(doc("tie-002",
                "banana banana alpha gamma banana",
                Map.of("title", "Tie case B")));

        // 长度相同但 banana 出现 4 次 => 对查询 "banana" 应排在 tie-00x 之前
        docs.add(doc("tie-003",
                "banana banana banana banana alpha",
                Map.of("title", "Tie case C")));

        // 同一词项重复，验证 BM25 的词频饱和：第 30 个 repeat 带来的边际增益很小
        docs.add(doc("repeat-001",
                "repeat ".repeat(30).trim(),
                Map.of("title", "Repeated term")));
        docs.add(doc("repeat-002",
                "repeat repeat",
                Map.of("title", "Repeated term twice")));

        docs.add(doc("numeric-1",
                "version v2 released token10 updated version notes",
                Map.of("title", "Numeric tokens")));

        docs.add(doc("cjk-1",
                "检索 search engine 引擎 local index index",
                Map.of("title", "CJK separators")));

        // ---- 确定性生成的批量文档 ----------------------------------------------

        String[] topics = {
                "index", "query", "ranking", "score", "token", "memory",
                "snapshot", "cursor", "filter", "lexicon"
        };
        String[] verbs = {
                "build", "update", "scan", "merge", "parse", "store", "rank", "flush"
        };

        for (int i = 1; i <= 60; i++) {
            String id = String.format("common-%04d", i);
            String topic = topics[i % topics.length];
            String verbA = verbs[i % verbs.length];
            String verbB = verbs[(i * 3) % verbs.length];

            // 每篇都含 "data"；再按编号制造词频与长度差异
            StringBuilder content = new StringBuilder();
            content.append("data data ");                      // 每篇至少 2 次 data
            content.append(topic).append(' ').append(topic).append(' ');
            content.append(verbA).append(' ').append(verbB).append(' ');
            if (i % 3 == 0) {
                content.append("data ");                       // 第 3 的倍数篇 data x3
            }
            if (i % 7 == 0) {
                content.append("ranking ranking ");            // 额外共享词，制造同分区
            }
            content.append("local retrieval corpus segment ").append(i);

            Map<String, String> meta = new LinkedHashMap<>();
            meta.put("title", "Corpus document " + i);
            meta.put("topic", topic);
            docs.add(doc(id, content.toString().trim(), meta));
        }

        return docs;
    }

    private static Document doc(String id, String content, Map<String, String> metadata) {
        return new Document(id, content, metadata);
    }
}
