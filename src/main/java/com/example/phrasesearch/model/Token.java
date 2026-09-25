package com.example.phrasesearch.model;

/**
 * 一个不可变的词元（token）。
 *
 * @param term        归一化后的词项（分析器负责大小写折叠等）
 * @param position    词在其所属 token 流中的 0 基位置序号；停用词被删除时该序号会形成“洞”
 * @param startOffset 词元在原始字段文本中的起始字符偏移（含）
 * @param endOffset   词元在原始字段文本中的结束字符偏移（不含）
 */
public record Token(String term, int position, int startOffset, int endOffset) {
}
