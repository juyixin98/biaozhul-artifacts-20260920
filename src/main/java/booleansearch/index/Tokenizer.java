package booleansearch.index;

import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 极简分词器：按字母/数字序列切词，统一转小写。
 * 标点、空白作为分隔符。纯 ASCII 正则，中文等非 ASCII 字符不会被当作词。
 */
public final class Tokenizer {

    private static final Pattern TOKEN_PATTERN = Pattern.compile("[A-Za-z0-9]+");

    private Tokenizer() {
    }

    public static List<String> tokenize(String text) {
        List<String> tokens = new ArrayList<>();
        if (text == null) {
            return tokens;
        }
        Matcher matcher = TOKEN_PATTERN.matcher(text);
        while (matcher.find()) {
            tokens.add(matcher.group().toLowerCase());
        }
        return tokens;
    }
}
