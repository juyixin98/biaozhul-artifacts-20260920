package com.example.topk.engine;

import com.example.topk.model.Row;

import java.util.List;
import java.util.Map;

/** Final rows for one group (already in result order) plus the exportable execution plan. */
public record GroupResult(String group, List<Row> rows) {}
