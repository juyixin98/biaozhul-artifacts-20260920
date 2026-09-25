package com.example.phrasesearch.analyze;

import com.example.phrasesearch.model.AnalyzedField;
import com.example.phrasesearch.model.Token;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/**
 * 带停用词表的分析器，展示“停用词不保留在 token 流中”的另一种明确语义。
 *
 * 关键规则（与 Lucene StopFilter 一致）：
 *  - 停用词从 token 流中删除；
 *  - 但【位置号不收紧】——停用词原本占用的位置形成空洞，后续普通词的 position
 *    仍按“停用词还在时本应有的序号”递增；positionCount 仍统计全部位置槽。
 *
 * 例："the quick brown fox"（the 是停用词）
 *   the   -> 删除，消耗位置槽 0
 *   quick -> 位置 1
 *   brown -> 位置 2
 *   fox   -> 位置 3
 *
 * 于是短语 "quick fox" 的位置差为 2（中间隔着被删的 the 的洞），
 * slop=0 的精确短语不命中，slop&gt;=1 才命中——空洞如实反映原文中被删词的存在。
 */
public final class StopGapAnalyzer implements Analyzer {

    /** 默认停用词表（刻意保持小型、确定；不含 that/with 等，避免干扰语料设计）。 */
    public static final Set<String> DEFAULT_STOPWORDS = Set.of(
            "a", "an", "the", "of", "in", "on", "and", "is", "are", "to", "for");

    private final Set<String> stopwords;

    public StopGapAnalyzer(Set<String> stopwords) {
        this.stopwords = Set.copyOf(stopwords);
    }

    public Set<String> stopwords() {
        return stopwords;
    }

    @Override
    public AnalyzedField analyze(String fieldName, String text) {
        List<Token> tokens = new ArrayList<>();
        if (text == null) {
            return new AnalyzedField(fieldName, "", tokens, 0);
        }
        int position = 0;
        int i = 0;
        int n = text.length();
        while (i < n) {
            while (i < n && !Character.isLetterOrDigit(text.charAt(i))) {
                i++;
            }
            int start = i;
            while (i < n && Character.isLetterOrDigit(text.charAt(i))) {
                i++;
            }
            if (start < i) {
                String term = text.substring(start, i).toLowerCase(Locale.ROOT);
                if (!stopwords.contains(term)) {
                    // 保留的 token 使用当前 position（被跳过的停用词在前面已消耗槽位）
                    tokens.add(new Token(term, position, start, i));
                }
                // 保留或删除都前进一步 -> 被删词留下“洞”
                position++;
            }
        }
        return new AnalyzedField(fieldName, text, List.copyOf(tokens), position);
    }

    @Override
    public String name() {
        return "stop_gap";
    }
}
