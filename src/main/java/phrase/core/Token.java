package phrase.core;

/** 一个分词结果：词项与其在被分析文本中的 1 基绝对位置。 */
public record Token(String term, int position) {
    public Token {
        if (term == null || term.isEmpty()) {
            throw new IllegalArgumentException("term must not be empty");
        }
        if (position < 1) {
            throw new IllegalArgumentException("position must be 1-based, got " + position);
        }
    }
}
