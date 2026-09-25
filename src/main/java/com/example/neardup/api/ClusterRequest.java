package com.example.neardup.api;

import com.example.neardup.Document;

import java.util.List;

/**
 * Request body for POST /cluster. {@code threshold} is optional; when null
 * the default from {@link com.example.neardup.NearDupConfig} is used.
 */
public record ClusterRequest(List<Document> documents, Double threshold) {
}
