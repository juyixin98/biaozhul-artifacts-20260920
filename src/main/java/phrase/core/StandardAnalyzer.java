package phrase.core;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * 标准分析器（无停用词表）：
 * <ul>
 *   <li>以 Unicode 字母/数字作为词字符，连续词字符构成一个词元，其余字符作为分隔符；</li>
 *   <li>词元整体转小写（Locale.ROOT，避免土耳其语 i 问题）；</li>
 *   <li>位置 1 基，每产出一个词元位置 +1，标点不占位置。</li>
 * </ul>
 */
public final class StandardAnalyzer implements Analyzer {

    @Override
    public String name() {
        return "standard";
    }

    @Override
    public List<Token> analyze(String text) {
        List<Token> out = new ArrayList<>();
        if (text == null) {
            return out;
        }
        int n = text.length();
        int i = 0;
        int position = 0;
        while (i < n) {
            while (i < n && !Character.isLetterOrDigit(text.charAt(i))) {
                i++;
            }
            int start = i;
            while (i < n && Character.isLetterOrDigit(text.charAt(i))) {
                i++;
            }
            if (i > start) {
                position++;
                String term = text.substring(start, i).toLowerCase(Locale.ROOT);
                out.add(new Token(term, position));
            }
        }
        return out;
    }
}
