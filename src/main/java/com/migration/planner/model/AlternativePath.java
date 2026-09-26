package com.migration.planner.model;

import java.util.List;

/** An equal-cost alternative path, reported when cost ties occur. */
public record AlternativePath(List<String> nodes, long totalCost) {
}
