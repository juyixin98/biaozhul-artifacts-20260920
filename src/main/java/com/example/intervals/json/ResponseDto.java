package com.example.intervals.json;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * The single response envelope for every request.
 *
 * <p>On success: {@code success=true, result=...}. On failure:
 * {@code success=false, error={code,message}}. Time-zone provenance is always
 * present so the version record travels with every output.
 */
@JsonInclude(JsonInclude.Include.ALWAYS)
public record ResponseDto(
        @JsonProperty("success") boolean success,
        @JsonProperty("domain") String domain,
        @JsonProperty("dataset") String dataset,
        @JsonProperty("result") ResultDto result,
        @JsonProperty("error") ErrorDto error,
        @JsonProperty("timezone") TzInfoDto timezone) {

    public static ResponseDto ok(String domain, String dataset, ResultDto result, TzInfoDto tz) {
        return new ResponseDto(true, domain, dataset, result, null, tz);
    }

    public static ResponseDto failure(String domain, String dataset, ErrorDto error, TzInfoDto tz) {
        return new ResponseDto(false, domain, dataset, null, error, tz);
    }
}
