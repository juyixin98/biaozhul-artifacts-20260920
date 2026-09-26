package com.example.monotime.scenario;

import com.example.monotime.tzdb.TzdbInfo;

import java.util.List;

/** 场景执行报告：环境信息 + 每一步结果（含当时双时钟快照）。 */
public record ScenarioReport(
        String scenarioName,
        String description,
        TzdbInfo tzdb,
        List<StepReport> steps,
        boolean aborted) {
}
