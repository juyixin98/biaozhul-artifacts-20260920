package com.example.segmenter.model;

import java.util.Collections;
import java.util.List;
import java.util.stream.Collectors;

/**
 * 一条完整的切分路径：覆盖原文的词序列及其总代价。
 */
public final class Segmentation {
    private final List<Token> tokens;
    private final double totalCost;
    private final int rank;

    public Segmentation(List<Token> tokens, double totalCost, int rank) {
        this.tokens = Collections.unmodifiableList(List.copyOf(tokens));
        this.totalCost = totalCost;
        this.rank = rank;
    }

    public List<Token> tokens() {
        return tokens;
    }

    public List<String> words() {
        return tokens.stream().map(Token::word).collect(Collectors.toUnmodifiableList());
    }

    public double totalCost() {
        return totalCost;
    }

    /** 在 N 最佳结果中的名次，从 1 开始。 */
    public int rank() {
        return rank;
    }

    public int tokenCount() {
        return tokens.size();
    }

    @Override
    public String toString() {
        return "#" + rank + " cost=" + totalCost + " "
                + tokens.stream().map(t -> t.unknown() ? "[" + t.word() + "]" : t.word())
                .collect(Collectors.joining(" / "));
    }
}
