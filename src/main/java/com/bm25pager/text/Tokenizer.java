package com.bm25pager.text;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * 固定分词规则（全局唯一实现，索引与查询共用）：
 *
 * 1. 以非 ASCII 字母/数字的字符作为分隔符，连续的 [A-Za-z0-9]+ 为一个 token；
 *    因此 CJK 等非 ASCII 字符一律被视为分隔符。
 * 2. 每个 token 转小写（Locale.ROOT，保证土耳其语等环境下结果稳定）。
 *
 * 注意：数字保留（如 "v2" -> ["v", "2"]）。该规则刻意保持简单且确定性，
 * 不做任何停用词过滤与词干化。
 */
public final class Tokenizer {

    private Tokenizer() {
    }

    public static List<String> tokenize(String text) {
        List<String> tokens = new ArrayList<>();
        if (text == null || text.isEmpty()) {
            return tokens;
        }
        int start = -1;
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            if (isTokenChar(c)) {
                if (start == -1) {
                    start = i;
                }
            } else if (start != -1) {
                tokens.add(lowercase(text, start, i));
                start = -1;
            }
        }
        if (start != -1) {
            tokens.add(lowercase(text, start, text.length()));
        }
        return tokens;
    }

    private static boolean isTokenChar(char c) {
        return (c >= 'a' && c <= 'z')
                || (c >= 'A' && c <= 'Z')
                || (c >= '0' && c <= '9');
    }

    private static String lowercase(String text, int start, int end) {
        return text.substring(start, end).toLowerCase(Locale.ROOT);
    }
}
