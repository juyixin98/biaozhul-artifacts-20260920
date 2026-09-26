package com.example.monotime.scenario;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;

import java.time.Instant;
import java.util.List;

/** 确定性场景定义：固定起始墙钟、起始单调刻度与步骤序列。 */
@JsonIgnoreProperties(ignoreUnknown = true)
public record ScenarioDefinition(
        String name,
        String description,
        Instant initialWallClock,
        Long initialMonotonicNanos,
        List<ScenarioStep> steps) {
}
