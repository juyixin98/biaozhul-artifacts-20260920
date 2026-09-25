package com.example.phrasesearch.analyze;

import com.example.phrasesearch.model.AnalyzedField;

/**
 * 文本分析器：把字段原文切成带位置、偏移的 token 流。
 * position 始终从 0 开始；不同实现的区别在于“被丢弃的 token 是否留下位置空洞”。
 */
public interface Analyzer {

    AnalyzedField analyze(String fieldName, String text);

    /** 分析器名（"standard" / "stop_gap"），会出现在服务响应里。 */
    String name();
}
