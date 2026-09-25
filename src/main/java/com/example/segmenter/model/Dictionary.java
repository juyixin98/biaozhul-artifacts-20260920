package com.example.segmenter.model;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 不可变词典快照。
 *
 * <p>每个词的代价 cost(w) = -ln(freq(w) / F)，F 为词频总和。
 * 版本可切换：{@link com.example.segmenter.api.Segmenter} 持有的是某一版快照，
 * 切换版本即构造/换用另一个快照对象。</p>
 */
public final class Dictionary {

    private final String version;
    private final Map<String, Double> costs;
    private final long totalFrequency;

    private Dictionary(String version, Map<String, Double> costs, long totalFrequency) {
        this.version = version;
        this.costs = Collections.unmodifiableMap(new LinkedHashMap<>(costs));
        this.totalFrequency = totalFrequency;
    }

    /** 由词频表构造，代价按 -ln(freq/F) 计算。 */
    public static Dictionary fromFrequencies(String version, Map<String, Long> frequencies) {
        long total = frequencies.values().stream().mapToLong(Long::longValue).sum();
        if (total <= 0) {
            throw new IllegalArgumentException("total frequency must be positive");
        }
        Map<String, Double> costs = new LinkedHashMap<>();
        for (Map.Entry<String, Long> e : frequencies.entrySet()) {
            String word = e.getKey();
            long f = e.getValue();
            if (f <= 0) {
                throw new IllegalArgumentException("frequency must be positive for word: " + word);
            }
            costs.put(word, -Math.log((double) f / (double) total));
        }
        return new Dictionary(version, costs, total);
    }

    /** 直接给定代价构造（主要用于测试确定性决胜规则）。 */
    public static Dictionary fromCosts(String version, Map<String, Double> explicitCosts) {
        Map<String, Double> copy = new LinkedHashMap<>(explicitCosts);
        copy.values().forEach(c -> {
            if (!Double.isFinite(c)) {
                throw new IllegalArgumentException("cost must be finite");
            }
        });
        return new Dictionary(version, copy, -1L);
    }

    public String version() {
        return version;
    }

    /** 未找到返回 null。 */
    public Double costOf(String word) {
        return costs.get(word);
    }

    public boolean contains(String word) {
        return costs.containsKey(word);
    }

    public int size() {
        return costs.size();
    }

    public long totalFrequency() {
        return totalFrequency;
    }

    public Map<String, Double> costsView() {
        return costs;
    }
}
