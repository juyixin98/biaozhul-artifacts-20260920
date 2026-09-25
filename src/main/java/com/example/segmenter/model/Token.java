package com.example.segmenter.model;

import java.util.Objects;

/**
 * 一个切分出来的词。
 *
 * @param word     词面文本（按 Unicode 码点切分，未知词为单字）
 * @param cost     该词在整条路径上的代价（词典词为 -ln(freq/F)，未知字为固定代价）
 * @param unknown  是否为未知单字词（未登录词按单字切出）
 */
public final class Token {
    private final String word;
    private final double cost;
    private final boolean unknown;

    public Token(String word, double cost, boolean unknown) {
        this.word = Objects.requireNonNull(word, "word");
        if (word.isEmpty()) {
            throw new IllegalArgumentException("token word must not be empty");
        }
        this.cost = cost;
        this.unknown = unknown;
    }

    public String word() {
        return word;
    }

    public double cost() {
        return cost;
    }

    public boolean unknown() {
        return unknown;
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (!(o instanceof Token)) return false;
        Token token = (Token) o;
        return unknown == token.unknown
                && Double.compare(token.cost, cost) == 0
                && word.equals(token.word);
    }

    @Override
    public int hashCode() {
        return Objects.hash(word, cost, unknown);
    }

    @Override
    public String toString() {
        return (unknown ? "UNK(" : "WORD(") + word + ", cost=" + cost + ")";
    }
}
