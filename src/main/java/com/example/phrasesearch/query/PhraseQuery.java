package com.example.phrasesearch.query;

import java.util.List;

/**
 * 一次短语查询请求。
 *
 * @param terms       短语词项序列（已由分析器归一化），长度至少为 1
 * @param slop        允许的最大位置间隙冗余量，固定非负整数，语义见 Searcher
 * @param field       字段名；null 表示跨字段（按文档全局拼接位置流匹配）
 * @param inOrder     true=词项必须按顺序出现（本项目固定支持的模式）
 * @param rawText     查询原文（回显用）
 */
public record PhraseQuery(List<String> terms, int slop, String field, boolean inOrder, String rawText) {

    public PhraseQuery {
        terms = List.copyOf(terms);
        if (terms.isEmpty()) {
            throw new IllegalArgumentException("phrase query must contain at least one term");
        }
        if (slop < 0) {
            throw new IllegalArgumentException("slop must be >= 0");
        }
    }
}
