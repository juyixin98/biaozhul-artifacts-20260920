package com.example.topk.engine;

import com.example.topk.model.Row;

import java.util.List;
import java.util.Map;

/**
 * Result of a grouped top-k execution:
 * groups sorted by group name (stable output), the normalized input rows and
 * a JSON-serializable execution plan.
 */
public record QueryResult(List<GroupResult> groups, List<Row> normalizedData, Map<String, Object> plan) {}
