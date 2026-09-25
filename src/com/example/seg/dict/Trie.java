package com.example.seg.dict;

import java.util.HashMap;
import java.util.Map;

/**
 * 词典前缀树。节点上记录以该节点结尾的词的放大整数代价；
 * 代价为 null 表示该节点只是前缀、不是一个词。
 */
public final class Trie {

    private final Node root = new Node();
    private int maxWordLength = 0;

    private static final class Node {
        final Map<Character, Node> children = new HashMap<>();
        Long cost; // 词终代价；null = 非词终
    }

    /** 插入一个词及其放大整数代价。重复插入以最后一次为准（方便测试构造）。 */
    public void put(String word, long scaledCost) {
        if (word == null || word.isEmpty()) {
            throw new IllegalArgumentException("词典词不能为空串");
        }
        Node node = root;
        for (int i = 0; i < word.length(); i++) {
            node = node.children.computeIfAbsent(word.charAt(i), k -> new Node());
        }
        node.cost = scaledCost;
        maxWordLength = Math.max(maxWordLength, word.length());
    }

    public boolean contains(String word) {
        Node node = walk(word);
        return node != null && node.cost != null;
    }

    public Long getCost(String word) {
        Node node = walk(word);
        return node == null ? null : node.cost;
    }

    private Node walk(String word) {
        Node node = root;
        for (int i = 0; i < word.length(); i++) {
            node = node.children.get(word.charAt(i));
            if (node == null) {
                return null;
            }
        }
        return node;
    }

    public int maxWordLength() {
        return maxWordLength;
    }

    /**
     * 从 text 的 begin 位置出发，枚举所有能命中的词，回调 (长度, 放大代价)。
     * 长度按短到长回调（trie 天然按遍历发现顺序，调用方不依赖其顺序）。
     */
    public void matchFrom(String text, int begin, WordSink sink) {
        Node node = root;
        int limit = Math.min(text.length(), begin + maxWordLength);
        for (int i = begin; i < limit; i++) {
            node = node.children.get(text.charAt(i));
            if (node == null) {
                break;
            }
            if (node.cost != null) {
                sink.accept(i + 1 - begin, node.cost);
            }
        }
    }

    /** 匹配回调：len 为词的字符长度，scaledCost 为放大整数代价。 */
    @FunctionalInterface
    public interface WordSink {
        void accept(int len, long scaledCost);
    }
}
