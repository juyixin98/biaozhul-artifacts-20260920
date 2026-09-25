package com.example.phrasesearch.model;

/**
 * 全局位置流中的一个词元（文档内所有字段按顺序拼接后的视角）。
 *
 * @param term           归一化词项
 * @param globalPosition 在文档全局拼接 token 流中的位置
 * @param field          所属字段名
 * @param localPosition  在所属字段内的局部位置
 * @param startOffset    字段原文起始字符偏移（含）
 * @param endOffset      字段原文结束字符偏移（不含）
 */
public record GlobalToken(String term,
                          int globalPosition,
                          String field,
                          int localPosition,
                          int startOffset,
                          int endOffset) {
}
