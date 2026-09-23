package com.example.topk.model;

/**
 * A normalized data row. {@code seq} is a stable, globally unique sequence
 * number assigned at ingest time; it is the final tie-breaker that makes the
 * row ordering total and therefore the whole computation deterministic.
 */
public record Row(String group, long value, long seq) {}
