package com.example.monotime.scenario;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;

import java.time.Instant;

/**
 * 场景脚本中的一步。字段按 {@link #op()} 取用，其余为 null。
 *
 * <ul>
 *   <li>{@code schedule}：timeoutId + ruleId(+version)</li>
 *   <li>{@code status}：timeoutId</li>
 *   <li>{@code delete}：timeoutId</li>
 *   <li>{@code advance}：真实流逝——只推进单调计时器，取 durationIso 或 nanos</li>
 *   <li>{@code adjustWallClock}：校时——只动墙钟，取 forwardIso/backwardIso/instant</li>
 *   <li>{@code restart}：JVM 重启，取 newMonotonicNanos/newWallClockInstant（均可缺省）</li>
 * </ul>
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public record ScenarioStep(
        String op,
        String timeoutId,
        String ruleId,
        String version,
        Long nanos,
        String durationIso,
        String forwardIso,
        String backwardIso,
        Instant instant,
        Long newMonotonicNanos,
        Instant newWallClockInstant,
        String note) {
}
