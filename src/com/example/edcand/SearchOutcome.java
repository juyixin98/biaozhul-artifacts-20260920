package com.example.edcand;

import java.util.List;

/**
 * 一次候选筛选搜索的结果与可观测统计量。
 *
 * @param matches        精算确认后的匹配（距离 &le; threshold），按距离、词项排序
 * @param totalTerms     索引词项总数
 * @param passedLengthGate 长度差 &le; threshold、进入重叠度打分的词项数
 * @param candidates     重叠度达到下界、被送去精算 Levenshtein 的候选数
 * @param scannedExact   实际执行码点 Levenshtein 精算的次数
 */
public record SearchOutcome(
        List<Match> matches,
        int totalTerms,
        int passedLengthGate,
        int candidates,
        int scannedExact) {

    /**
     * @param term     命中的（已规范化）词项
     * @param original 构建索引时提供的原始词项（可能为 null，表示与 term 相同）
     * @param distance 精确码点 Levenshtein 距离
     */
    public record Match(String term, String original, int distance) {
    }
}
