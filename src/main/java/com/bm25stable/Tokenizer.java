package com.bm25stable;

import java.util.ArrayList;
import java.util.List;

/**
 * 固定分词规则（不随配置变化）：
 * <ol>
 *   <li>全文转小写；</li>
 *   <li>按“连续字母或数字”切分，即 [a-z0-9]+ 为一个词元，其余字符一律视为分隔符；</li>
 *   <li>不做词干提取、不去停用词；</li>
 *   <li>空文本 / 全分隔符文本得到空词元列表。</li>
 * </ol>
 */
public final class Tokenizer {

    private Tokenizer() {
    }

    public static List<String> tokenize(String text) {
        List<String> tokens = new ArrayList<>();
        if (text == null || text.isEmpty()) {
            return tokens;
        }
        String lower = text.toLowerCase();
        StringBuilder current = new StringBuilder();
        for (int i = 0; i < lower.length(); i++) {
            char c = lower.charAt(i);
            if (isTokenChar(c)) {
                current.append(c);
            } else if (current.length() > 0) {
                tokens.add(current.toString());
                current.setLength(0);
            }
        }
        if (current.length() > 0) {
            tokens.add(current.toString());
        }
        return tokens;
    }

    private static boolean isTokenChar(char c) {
        return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9');
    }
}
