package com.example.intervals.engine;

import com.example.intervals.algebra.IntervalAlgebra;
import com.example.intervals.error.ErrorCode;
import com.example.intervals.error.IntervalException;
import com.example.intervals.fixed.DatasetDto;
import com.example.intervals.fixed.DatasetRepository;
import com.example.intervals.json.IntervalConverter;
import com.example.intervals.json.IntervalDto;
import com.example.intervals.json.RequestDto;
import com.example.intervals.json.ResponseDto;
import com.example.intervals.json.ResultDto;
import com.example.intervals.json.TzInfoDto;
import com.example.intervals.model.Interval;
import com.example.intervals.model.IntervalSet;
import com.example.intervals.tz.TimeZoneInfo;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * The request-processing boundary: turns a parsed {@link RequestDto} into a
 * {@link ResponseDto}, performing all validation and set-algebra evaluation.
 * This class contains no JSON (de)serialization or I/O itself.
 */
public final class IntervalService {

    private final DatasetRepository datasets;
    private final TimeZoneInfo timeZoneInfo;

    public IntervalService(DatasetRepository datasets, TimeZoneInfo timeZoneInfo) {
        this.datasets = datasets;
        this.timeZoneInfo = timeZoneInfo;
    }

    public TzInfoDto tzInfo() {
        return new TzInfoDto(
                timeZoneInfo.jreTzDataVersion(),
                timeZoneInfo.osTzDataVersion(),
                timeZoneInfo.javaVersion(),
                timeZoneInfo.javaVendor(),
                timeZoneInfo.zoneCount(),
                List.copyOf(timeZoneInfo.sampleZones()));
    }

    public ResponseDto process(RequestDto request) {
        String domainId = request == null ? null : request.domain();
        String datasetId = request == null ? null : request.dataset();
        try {
            Domain<?> domain = Domains.require(domainId);
            return processFor(domain, request);
        } catch (IntervalException e) {
            return ResponseDto.failure(domainId, datasetId,
                    new com.example.intervals.json.ErrorDto(e.errorCode().code(), e.getMessage()),
                    tzInfo());
        }
    }

    private <T extends Comparable<? super T>> ResponseDto processFor(
            Domain<T> domain, RequestDto request) {
        Map<String, IntervalSet<T>> inputs = resolveInputs(domain, request);
        ExpressionEvaluator<T> evaluator = new ExpressionEvaluator<>(inputs);
        IntervalSet<T> raw = evaluator.evaluate(request.expression());

        // Re-normalize defensively so the emitted contract always holds, even
        // if the evaluator internals change.
        IntervalSet<T> normalized = new IntervalAlgebra<T>().normalize(raw.intervals());

        IntervalConverter<T> converter = new IntervalConverter<>(domain);
        List<IntervalDto> out = converter.toDtoList(normalized.intervals());
        ResultDto result = new ResultDto(out, out.size(), normalized.isEmpty());
        return ResponseDto.ok(domain.id(), request.dataset(), result, tzInfo());
    }

    private <T extends Comparable<? super T>> Map<String, IntervalSet<T>> resolveInputs(
            Domain<T> domain, RequestDto request) {
        IntervalConverter<T> converter = new IntervalConverter<>(domain);
        Map<String, List<Interval<T>>> merged = new LinkedHashMap<>();

        if (request.dataset() != null) {
            DatasetDto dataset = datasets.require(request.dataset());
            if (!domain.id().equals(dataset.domain())) {
                throw new IntervalException(ErrorCode.UNKNOWN_DOMAIN,
                        "dataset '" + dataset.id() + "' is defined for domain '" + dataset.domain()
                                + "' but request domain is '" + domain.id() + "'");
            }
            for (Map.Entry<String, List<IntervalDto>> e : dataset.sets().entrySet()) {
                merged.put(e.getKey(), converter.toModelList(e.getValue()));
            }
        }

        if (request.sets() != null) {
            for (Map.Entry<String, List<IntervalDto>> e : request.sets().entrySet()) {
                if (e.getKey() == null || e.getKey().isBlank()) {
                    throw new IntervalException(ErrorCode.INVALID_REQUEST,
                            "inline set name must be a non-empty string");
                }
                merged.put(e.getKey(), converter.toModelList(e.getValue()));
            }
        }

        // Normalize every input once so leaf references are canonical.
        IntervalAlgebra<T> algebra = new IntervalAlgebra<>();
        Map<String, IntervalSet<T>> result = new LinkedHashMap<>();
        for (Map.Entry<String, List<Interval<T>>> e : merged.entrySet()) {
            result.put(e.getKey(), algebra.normalize(e.getValue()));
        }
        return result;
    }
}
