package boolsearch;

import java.util.ArrayList;
import java.util.List;

/** 文档分词：小写化，按非字母数字切分。 */
public final class Tokenizer {
    private Tokenizer() {}

    public static List<String> tokenize(String text) {
        List<String> out = new ArrayList<>();
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            if (Character.isLetterOrDigit(c)) {
                sb.append(Character.toLowerCase(c));
            } else if (sb.length() > 0) {
                out.add(sb.toString());
                sb.setLength(0);
            }
        }
        if (sb.length() > 0) out.add(sb.toString());
        return out;
    }
}
