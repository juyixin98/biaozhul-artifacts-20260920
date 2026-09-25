package com.example.phrasesearch.analyze;

import com.example.phrasesearch.model.AnalyzedField;
import com.example.phrasesearch.model.Token;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * 标准分析器（项目默认）。
 *
 * 规则：
 *  - 按 Unicode 字母/数字（{@link Character#isLetterOrDigit}）的连续段切词；
 *  - 大小写折叠为小写（Locale.ROOT）；
 *  - 不删除任何词，因此【停用词被保留并占据位置】。
 *
 * 这是本项目对“停用词是否保留位置”给出的明确答案：
 * 默认模式下停用词与普通词完全等价，positionCount 等于 token 数，没有位置空洞。
 */
public final class StandardAnalyzer implements Analyzer {

    @Override
    public AnalyzedField analyze(String fieldName, String text) {
        List<Token> tokens = new ArrayList<>();
        if (text == null) {
            return new AnalyzedField(fieldName, "", tokens, 0);
        }
        int position = 0;
        int i = 0;
        int n = text.length();
        while (i < n) {
            while (i < n && !Character.isLetterOrDigit(text.charAt(i))) {
                i++;
            }
            int start = i;
            while (i < n && Character.isLetterOrDigit(text.charAt(i))) {
                i++;
            }
            if (start < i) {
                String term = text.substring(start, i).toLowerCase(Locale.ROOT);
                tokens.add(new Token(term, position, start, i));
                position++;
            }
        }
        return new AnalyzedField(fieldName, text, List.copyOf(tokens), position);
    }

    @Override
    public String name() {
        return "standard";
    }
}
