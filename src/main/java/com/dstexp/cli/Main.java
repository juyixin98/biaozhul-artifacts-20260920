package com.dstexp.cli;

import com.dstexp.json.JsonMappers;
import com.dstexp.model.ErrorResponse;
import com.dstexp.model.ExpandRequest;
import com.dstexp.model.ExpandResponse;
import com.dstexp.service.ExpansionService;
import com.dstexp.service.RequestValidationException;
import com.dstexp.schedule.LocalTimeResolver;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.ZoneId;
import java.time.zone.ZoneRulesProvider;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Command-line entry point. Pure back-end, JSON in / JSON out, no network and no clock
 * dependency (all inputs are explicit in the request).
 *
 * <pre>
 *   java -jar dst-rule-expander.jar expand [request.json] [-o response.json]
 *   java -jar dst-rule-expander.jar tzdb
 *   java -jar dst-rule-expander.jar demo <spring|fall|cross-year|error>
 * </pre>
 * When the request file is omitted, the request is read from standard input.
 */
public final class Main {

    private static final ObjectMapper MAPPER = JsonMappers.mapper();
    private static final ExpansionService SERVICE = new ExpansionService();

    public static void main(String[] args) {
        System.exit(execute(args));
    }

    public static int execute(String[] args) {
        if (args.length == 0) {
            usage();
            return 64;
        }
        return switch (args[0]) {
            case "expand" -> runExpand(args);
            case "tzdb" -> runTzdb();
            case "demo" -> runDemo(args);
            case "-h", "--help", "help" -> {
                usage();
                yield 0;
            }
            default -> {
                System.err.println("Unknown command: " + args[0]);
                usage();
                yield 64;
            }
        };
    }

    private static int runExpand(String[] args) {
        Path input = null;
        Path output = null;
        for (int i = 1; i < args.length; i++) {
            switch (args[i]) {
                case "-o", "--output" -> {
                    if (i + 1 >= args.length) {
                        return failUsage("Missing value for " + args[i]);
                    }
                    output = Path.of(args[++i]);
                }
                case "-h", "--help" -> {
                    usage();
                    return 0;
                }
                default -> {
                    if (args[i].startsWith("-")) {
                        return failUsage("Unknown option: " + args[i]);
                    }
                    if (input != null) {
                        return failUsage("Multiple input files given");
                    }
                    input = Path.of(args[i]);
                }
            }
        }

        String json;
        try {
            json = input == null
                    ? new String(System.in.readAllBytes(), StandardCharsets.UTF_8)
                    : Files.readString(input, StandardCharsets.UTF_8);
        } catch (IOException e) {
            return failIo("Cannot read request: " + e.getMessage());
        }

        ExpandRequest request;
        try {
            request = MAPPER.readValue(json, ExpandRequest.class);
        } catch (IOException e) {
            return writeError(output, ErrorResponse.of("INVALID_JSON",
                    "Request is not valid JSON: " + rootCauseMessage(e)));
        }

        try {
            ExpandResponse response = SERVICE.expand(request);
            return writeResponse(output, MAPPER.writerWithDefaultPrettyPrinter()
                    .writeValueAsString(response));
        } catch (RequestValidationException e) {
            return writeError(output, ErrorResponse.of(e.code(), e.getMessage()));
        } catch (com.dstexp.schedule.LocalTimeResolver.AmbiguousTimeException e) {
            return writeError(output, ErrorResponse.of(
                    e.gap ? "GAP_ENCOUNTERED" : "OVERLAP_ENCOUNTERED", e.getMessage()));
        } catch (Exception e) {
            return writeError(output, ErrorResponse.of("INTERNAL_ERROR",
                    "Unexpected failure: " + rootCauseMessage(e)));
        }
    }

    private static int runTzdb() {
        // The TZDB version is global; use a region-based zone (UTC may carry no versions).
        String tzdb = ZoneRulesProvider.getVersions("America/New_York").lastKey();
        Map<String, Object> info = new LinkedHashMap<>();
        info.put("success", true);
        info.put("tzdbVersion", tzdb);
        info.put("javaVersion", System.getProperty("java.version"));
        info.put("javaVendor", System.getProperty("java.vendor"));
        info.put("availableZoneCount", ZoneId.getAvailableZoneIds().size());
        try {
            System.out.println(MAPPER.writerWithDefaultPrettyPrinter().writeValueAsString(info));
            return 0;
        } catch (IOException e) {
            System.err.println("Failed to serialize tzdb info: " + e.getMessage());
            return 1;
        }
    }

    private static int runDemo(String[] args) {
        if (args.length < 2) {
            return failUsage("demo requires one of: spring, fall, cross-year, error");
        }
        String file = switch (args[1]) {
            case "spring" -> "spring-gap.json";
            case "fall" -> "fall-overlap.json";
            case "cross-year" -> "cross-year.json";
            case "error" -> "error-policies.json";
            default -> null;
        };
        if (file == null) {
            return failUsage("Unknown demo dataset: " + args[1]
                    + " (expected spring|fall|cross-year|error)");
        }
        String resource = "/demo/" + file;
        try (InputStream in = Main.class.getResourceAsStream(resource)) {
            if (in == null) {
                System.err.println("Bundled demo data missing: " + resource);
                return 70;
            }
            String json = new String(in.readAllBytes(), StandardCharsets.UTF_8);
            ExpandRequest request = MAPPER.readValue(json, ExpandRequest.class);
            ExpandResponse response = SERVICE.expand(request);
            System.out.println(MAPPER.writerWithDefaultPrettyPrinter().writeValueAsString(response));
            return 0;
        } catch (RequestValidationException e) {
            return writeError(null, ErrorResponse.of(e.code(), e.getMessage()));
        } catch (LocalTimeResolver.AmbiguousTimeException e) {
            return writeError(null, ErrorResponse.of(
                    e.gap ? "GAP_ENCOUNTERED" : "OVERLAP_ENCOUNTERED", e.getMessage()));
        } catch (IOException e) {
            System.err.println("Demo failed: " + e.getMessage());
            return 1;
        }
    }

    private static int writeResponse(Path output, String json) {
        try {
            if (output == null) {
                System.out.println(json);
            } else {
                Files.writeString(output, json + System.lineSeparator(), StandardCharsets.UTF_8);
            }
            return 0;
        } catch (IOException e) {
            return failIo("Cannot write response: " + e.getMessage());
        }
    }

    private static int writeError(Path output, ErrorResponse error) {
        try {
            String json = MAPPER.writerWithDefaultPrettyPrinter().writeValueAsString(error);
            if (output == null) {
                System.err.println(json);
            } else {
                Files.writeString(output, json + System.lineSeparator(), StandardCharsets.UTF_8);
            }
            return 2;
        } catch (IOException e) {
            System.err.println("Failed to serialize error: " + e.getMessage());
            return 1;
        }
    }

    private static int failUsage(String message) {
        System.err.println(message);
        usage();
        return 64;
    }

    private static int failIo(String message) {
        System.err.println(message);
        return 74;
    }

    private static String rootCauseMessage(Throwable t) {
        Throwable cur = t;
        while (cur.getCause() != null && cur.getCause() != cur) {
            cur = cur.getCause();
        }
        return cur.getMessage() == null ? cur.getClass().getSimpleName() : cur.getMessage();
    }

    private static void usage() {
        System.err.println("""
                Usage:
                  dst-rule-expander expand [request.json] [-o response.json]
                      Expand rules into UTC occurrences. Reads stdin when no file is given.
                  dst-rule-expander tzdb
                      Print the bundled IANA time-zone database version and runtime info.
                  dst-rule-expander demo <spring|fall|cross-year|error>
                      Run one of the fixed local test datasets bundled with the jar.

                Request JSON fields:
                  zoneId        IANA zone, e.g. America/New_York      (required)
                  fromDate      inclusive local date yyyy-MM-dd       (required)
                  toDate        inclusive local date yyyy-MM-dd       (required)
                  rules[]       daily {ruleId,type:"daily",localTime:"HH:mm[:ss]"}
                                cron  {ruleId,type:"cron",cron:"m h dom mon dow"}
                  gapPolicy     earlier|later|skip|error   (default earlier)
                  overlapPolicy earlier|later|skip|error   (default later)
                  sort          boolean, default true
                  deduplicate   boolean, default true
                  limit         optional positive integer
                """);
    }

    private Main() {
    }
}
