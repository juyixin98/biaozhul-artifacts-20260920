package com.example.phrasesearch.model;

import java.util.List;

/**
 * 一篇文档在索引中的形态。
 * globalTokens 按字段声明顺序拼接，字段内按局部位置有序，
 * 因此整体严格按 globalPosition 递增。
 */
public record IndexedDoc(int docId,
                         String externalId,
                         List<String> fieldOrder,
                         List<GlobalToken> globalTokens) {
}
