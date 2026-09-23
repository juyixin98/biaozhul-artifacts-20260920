package com.example.topk.model;

/**
 * A row as supplied by the caller, before normalization.
 * {@code seq} may be null, in which case the engine assigns sequence numbers
 * in input order. If any row carries a seq, all rows must, and they must be unique.
 */
public record RawRow(String group, long value, Long seq) {}
