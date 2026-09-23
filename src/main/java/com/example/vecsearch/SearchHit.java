package com.example.vecsearch;

import java.util.List;
import java.util.Map;

/** One ranked hit. */
public record SearchHit(String id, double distance, Map<String, Object> metadata) {}
