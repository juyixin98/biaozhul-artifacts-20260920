package com.example.intervals.cli;

import com.example.intervals.engine.IntervalService;
import com.example.intervals.error.ErrorCode;
import com.example.intervals.error.IntervalException;
import com.example.intervals.fixed.DatasetDto;
import com.example.intervals.fixed.DatasetRepository;
import com.example.intervals.json.ErrorDto;
import com.example.intervals.json.RequestDto;
import com.example.intervals.json.ResponseDto;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.example.intervals.tz.TimeZoneInfo;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Command-line entry point. Pure backend: no HTTP server and no UI.
 *
 * <pre>
 *   java -jar interval-set-algebra.jar                 # read one request JSON from stdin
 *   java -jar ... --file path/to/request.json          # read request from a file
 *   java -jar ... datasets                             # list bundled fixed datasets as JSON
 *   java -jar ... timezone                             # print time-zone database provenance as JSON
 *   java -jar ... help                                 # usage
 * </pre>
 *
 * Every computed answer is printed to stdout as the {@link ResponseDto} JSON
 * envelope. On any rejected request {@code success=false} is printed and the
 * process exits with status 1; usage errors exit with status 2.
 */
public final class Main {

    private final ObjectMapper mapper;
    private final DatasetRepository datasets;
    private final TimeZoneInfo tz;

    Main() {
        this.mapper = new ObjectMapper().enable(SerializationFeature.INDENT_OUTPUT);
        this.datasets = DatasetRepository.loadDefault();
        this.tz = TimeZoneInfo.detect();
    }

    public static void main(String[] args) {
        Main app = new Main();
        int exit = app.run(args);
        System.exit(exit);
    }

    int run(String[] args) {
        if (args.length == 0) {
            return handleRequest(null);
        }
        return switch (args[0]) {
            case "--file" -> {
                if (args.length != 2) {
                    System.err.println("usage: --file <request.json>");
                    yield 2;
                }
                yield handleRequest(Path.of(args[1]));
            }
            case "datasets" -> listDatasets();
            case "timezone" -> printTimezone();
            case "help", "--help", "-h" -> {
                printUsage(System.out);
                yield 0;
            }
            default -> {
                System.err.println("unknown argument: " + args[0]);
                System.err.println();
                printUsage(System.err);
                yield 2;
            }
        };
    }

    private int handleRequest(Path file) {
        String raw;
        try {
            raw = file == null
                    ? new String(System.in.readAllBytes(), StandardCharsets.UTF_8)
                    : Files.readString(file, StandardCharsets.UTF_8);
        } catch (IOException e) {
            return emitFailure(null, new IntervalException(ErrorCode.INVALID_REQUEST,
                    "cannot read request: " + e.getMessage(), e));
        }

        RequestDto request;
        try {
            request = mapper.readValue(raw, RequestDto.class);
        } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
            return emitFailure(null, new IntervalException(ErrorCode.INVALID_REQUEST,
                    "request is not valid JSON: " + e.getOriginalMessage(), e));
        }

        try {
            IntervalService service = new IntervalService(datasets, tz);
            ResponseDto response = service.process(request);
            write(response);
            return response.success() ? 0 : 1;
        } catch (IntervalException e) {
            return emitFailure(request, e);
        } catch (RuntimeException e) {
            return emitFailure(request, new IntervalException(ErrorCode.INTERNAL_ERROR,
                    "unexpected error: " + e, e));
        }
    }

    private int emitFailure(RequestDto request, IntervalException error) {
        String domain = request == null ? null : request.domain();
        String dataset = request == null ? null : request.dataset();
        IntervalService service = new IntervalService(datasets, tz);
        ErrorDto errorDto = new ErrorDto(error.errorCode().code(), error.getMessage());
        ResponseDto response = ResponseDto.failure(domain, dataset, errorDto, service.tzInfo());
        write(response);
        return 1;
    }

    private int listDatasets() {
        java.util.List<Map<String, Object>> out = new java.util.ArrayList<>();
        for (DatasetDto ds : datasets.all()) {
            Map<String, Object> entry = new LinkedHashMap<>();
            entry.put("id", ds.id());
            entry.put("domain", ds.domain());
            entry.put("description", ds.description());
            entry.put("setNames", ds.sets().keySet());
            out.add(entry);
        }
        Map<String, Object> root = new LinkedHashMap<>();
        root.put("timezone", tzSummary());
        root.put("datasets", out);
        write(root);
        return 0;
    }

    private int printTimezone() {
        write(tzSummary());
        return 0;
    }

    private Map<String, Object> tzSummary() {
        Map<String, Object> info = new LinkedHashMap<>();
        info.put("jreTzDataVersion", tz.jreTzDataVersion());
        info.put("osTzDataVersion", tz.osTzDataVersion());
        info.put("javaVersion", tz.javaVersion());
        info.put("javaVendor", tz.javaVendor());
        info.put("availableZoneCount", tz.zoneCount());
        info.put("sampleZones", tz.sampleZones());
        return info;
    }

    private void printUsage(java.io.PrintStream out) {
        out.println("""
                Interval Set Algebra (pure backend, JSON in / JSON out)

                Usage:
                  java -jar interval-set-algebra.jar                       Read one request JSON from stdin
                  java -jar interval-set-algebra.jar --file request.json   Read request JSON from a file
                  java -jar interval-set-algebra.jar datasets              List bundled fixed datasets
                  java -jar interval-set-algebra.jar timezone              Print time-zone database provenance
                  java -jar interval-set-algebra.jar help                  This help

                Exit codes: 0 success, 1 rejected/invalid request, 2 usage error.
                See README.md for the request schema and samples/ for examples.""");
    }

    private void write(Object value) {
        try {
            mapper.writeValue(System.out, value);
            System.out.println();
        } catch (IOException e) {
            throw new IllegalStateException("failed to write JSON to stdout", e);
        }
    }
}
