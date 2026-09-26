package com.example.intervals.fixed;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;

/** Root of {@code data/datasets.json}. */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record DatasetCatalogDto(@JsonProperty("datasets") List<DatasetDto> datasets) {
}
