package com.example.phrasesearch.model;

import java.util.List;

/**
 * 一个字段经分析器切词后的结果。
 *
 * @param name          字段名
 * @param text          字段原始文本
 * @param tokens        保留下来的 token 流（stop_gap 模式下不含停用词）
 * @param positionCount 该字段消耗的位置槽总数（含被删除停用词的位置）。
 *                      标准模式等于 tokens.size()；stop_gap 模式可能更大，
 *                      跨字段拼接全局位置时必须用它而不是 tokens.size()。
 */
public record AnalyzedField(String name, String text, List<Token> tokens, int positionCount) {
}
