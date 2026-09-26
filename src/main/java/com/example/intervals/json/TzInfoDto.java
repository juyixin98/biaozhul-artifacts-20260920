package com.example.intervals.json;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;

/**
 * Time-zone database provenance included in every response.
 */
@JsonInclude(JsonInclude.Include.ALWAYS)
public record TzInfoDto(
        @JsonProperty("jreTzDataVersion") String jreTzDataVersion,
        @JsonProperty("osTzDataVersion") String osTzDataVersion,
        @JsonProperty("javaVersion") String javaVersion,
        @JsonProperty("javaVendor") String javaVendor,
        @JsonProperty("availableZoneCount") int availableZoneCount,
        @JsonProperty("sampleZones") List<String> sampleZones) {
}
