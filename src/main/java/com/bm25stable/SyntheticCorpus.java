package com.bm25stable;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 自建合成语料：服务启动时装载，内容固定、可复现。
 * 特意包含：空文档、仅标点文档、重复词文档、同分文档组、多词共现文档，
 * 便于手工核对检索与分页行为。
 */
public final class SyntheticCorpus {

    private SyntheticCorpus() {
    }

    public static Map<String, String> documents() {
        Map<String, String> docs = new LinkedHashMap<>();

        // --- 主题：apple / fruit ---
        docs.put("doc-01", "apple apple apple");
        docs.put("doc-02", "apple banana");
        docs.put("doc-03", "apple orange grape");
        docs.put("doc-04", "banana orange grape melon");
        docs.put("doc-05", "the apple pie recipe uses fresh apple and cinnamon");

        // --- 同分组：三篇内容完全相同，得分必须相等，按 docId 升序决胜 ---
        docs.put("dup-1", "cherry cherry");
        docs.put("dup-2", "cherry cherry");
        docs.put("dup-3", "cherry cherry");

        // --- 主题：space ---
        docs.put("doc-06", "space rocket launch");
        docs.put("doc-07", "rocket engine fuel");
        docs.put("doc-08", "space station orbit");
        docs.put("doc-09", "orbit satellite space");

        // --- 混合词频 ---
        docs.put("doc-10", "java java java programming language");
        docs.put("doc-11", "java programming");
        docs.put("doc-12", "programming language design");
        docs.put("doc-13", "rust programming language");

        // --- 边界情况 ---
        docs.put("empty-1", "");                 // 空文档：零词元
        docs.put("punct-1", "!!! ??? ...");      // 仅分隔符：分词后同样为空
        docs.put("case-1", "Apple APPLE aPpLe"); // 大小写混合：应归一为 apple x3
        docs.put("num-1", "java 21 release 2023"); // 数字词元

        // --- 长文档 ---
        docs.put("doc-14", "search engine index token query score rank page cursor snapshot "
                + "search engine bm25 term frequency inverse document frequency");

        return docs;
    }
}
