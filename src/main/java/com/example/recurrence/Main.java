package com.example.recurrence;

import com.example.recurrence.core.ExpansionLimitExceededException;
import com.example.recurrence.core.RecurrenceExpander;
import com.example.recurrence.core.ValidationException;
import com.example.recurrence.json.RequestParser;
import com.example.recurrence.json.ResponseWriter;
import com.fasterxml.jackson.databind.JsonNode;

import java.io.IOException;
import java.io.InputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.LocalDate;
import java.util.List;

/**
 * Command-line entry point: reads one JSON request (file path argument, or
 * stdin when no path is given), expands the rule, and writes the JSON
 * response to stdout.
 *
 * <pre>
 *   java -jar target/recurrence-expander-1.0.0.jar examples/request-daily.json
 *   cat request.json | java -jar target/recurrence-expander-1.0.0.jar
 * </pre>
 *
 * Exit code 0 = success, 2 = request/validation/limit error (the JSON error
 * document is still printed to stdout).
 */
public final class Main {

    static final int EXIT_OK = 0;
    static final int EXIT_ERROR = 2;

    private Main() {
    }

    public static void main(String[] args) {
        System.exit(run(args, System.in, System.out));
    }

    /** Executes one request and returns the process exit code (testable). */
    static int run(String[] args, InputStream in, PrintStream out) {
        String requestId = null;
        try {
            String json = readInput(args, in);
            JsonNode root = ResponseWriter.mapper().readTree(json);
            requestId = root != null && root.isObject() && root.hasNonNull("requestId")
                    ? root.get("requestId").asText() : null;

            RequestParser.ExpandRequest request = RequestParser.parse(root);
            List<LocalDate> occurrences = new RecurrenceExpander().expand(
                    request.rule(),
                    request.windowStart(),
                    request.windowEnd(),
                    request.effectiveMaxExpansions());

            out.println(ResponseWriter.success(request.requestId(), occurrences));
            return EXIT_OK;
        } catch (ExpansionLimitExceededException e) {
            out.println(ResponseWriter.error(requestId, "EXPANSION_LIMIT_EXCEEDED", e.getMessage()));
            return EXIT_ERROR;
        } catch (ValidationException e) {
            out.println(ResponseWriter.error(requestId, e.code(), e.getMessage()));
            return EXIT_ERROR;
        } catch (IOException | IllegalArgumentException e) {
            // Unparseable JSON or unreadable input.
            out.println(ResponseWriter.error(requestId, "INVALID_JSON",
                    "request is not valid JSON: " + e.getMessage()));
            return EXIT_ERROR;
        }
    }

    private static String readInput(String[] args, InputStream in) throws IOException {
        if (args.length > 0) {
            return Files.readString(Path.of(args[0]), StandardCharsets.UTF_8);
        }
        return new String(in.readAllBytes(), StandardCharsets.UTF_8);
    }
}
