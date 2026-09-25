package com.example.segmenter.model;

import java.util.HashMap;
import java.util.Map;

/**
 * 按 Unicode 码点组织的词典 Trie。
 * 每个节点记录从根到该节点是否构成词及其代价。
 */
public final class Trie {

    /** Trie 节点：从位置 i 沿原文码点行走时，terminal 命中即产生一条词典边。 */
    public static final class Node {
        private final Map<Integer, Node> children = new HashMap<>();
        private double cost;
        private boolean terminal;

        public Map<Integer, Node> children() {
            return children;
        }

        public boolean isTerminal() {
            return terminal;
        }

        public double cost() {
            return cost;
        }
    }

    private final Node root = new Node();
    private int wordCount = 0;

    public Trie(Dictionary dictionary) {
        for (Map.Entry<String, Double> e : dictionary.costsView().entrySet()) {
            insert(e.getKey(), e.getValue());
        }
    }

    private void insert(String word, double cost) {
        int[] cps = word.codePoints().toArray();
        if (cps.length == 0) {
            throw new IllegalArgumentException("dictionary contains empty word");
        }
        Node node = root;
        for (int cp : cps) {
            node = node.children.computeIfAbsent(cp, k -> new Node());
        }
        if (!node.terminal) {
            wordCount++;
        }
        node.terminal = true;
        node.cost = cost;
    }

    public int wordCount() {
        return wordCount;
    }

    public Node root() {
        return root;
    }
}
