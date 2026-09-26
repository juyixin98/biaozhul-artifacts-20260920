package com.example.monotime.data;

import com.example.monotime.domain.TimeRule;

import java.time.Instant;
import java.time.LocalTime;
import java.time.ZoneId;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * 本地固定测试数据目录：时间规则不来自数据库或外部服务，全部硬编码于此。
 *
 * <p>用 {@code ruleId + version} 选取；保留多版本是为了体现“版本化规则”，
 * 与 TZDB 版本一起记录在每次转换结果中。</p>
 */
public final class RuleCatalog {

    private final Map<Key, TimeRule> rules = new LinkedHashMap<>();

    public RuleCatalog() {
        add(new TimeRule.AbsoluteInstant(
                "brief-absolute", "1",
                Instant.parse("2026-09-25T10:05:00Z"),
                "固定绝对截止：演示当天 10:05Z"));
        add(new TimeRule.AbsoluteInstant(
                "brief-absolute", "2",
                Instant.parse("2026-11-01T17:30:00Z"),
                "固定绝对截止：落在美东夏令时结束（fall-back）当天"));
        add(new TimeRule.RelativeDuration(
                "grace-two-minutes", "1",
                java.time.Duration.parse("PT2M")));
        add(new TimeRule.DailyLocalCutoff(
                "daily-1730-newyork", "1",
                LocalTime.of(17, 30), ZoneId.of("America/New_York")));
        add(new TimeRule.DailyLocalCutoff(
                "daily-1800-shanghai", "1",
                LocalTime.of(18, 0), ZoneId.of("Asia/Shanghai")));
    }

    public Optional<TimeRule> find(String ruleId, String version) {
        return Optional.ofNullable(rules.get(new Key(ruleId, version)));
    }

    /** 未指定版本时取该 ruleId 的最高版本（按字符串排序，目录内均为整数版本号）。 */
    public Optional<TimeRule> findLatest(String ruleId) {
        return rules.keySet().stream()
                .filter(k -> k.ruleId().equals(ruleId))
                .max((a, b) -> compareVersions(a.version(), b.version()))
                .map(rules::get);
    }

    public List<TimeRule> all() {
        return List.copyOf(rules.values());
    }

    private void add(TimeRule rule) {
        rules.put(new Key(rule.ruleId(), rule.version()), rule);
    }

    private static int compareVersions(String a, String b) {
        try {
            return Integer.compare(Integer.parseInt(a), Integer.parseInt(b));
        } catch (NumberFormatException e) {
            return a.compareTo(b);
        }
    }

    private record Key(String ruleId, String version) {
    }
}
