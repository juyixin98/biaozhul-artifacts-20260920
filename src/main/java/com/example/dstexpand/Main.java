package com.example.dstexpand;

import com.example.dstexpand.engine.DstExpander;
import com.example.dstexpand.engine.ExpansionException;
import com.example.dstexpand.json.Json;
import com.example.dstexpand.model.ExpandRequest;
import com.example.dstexpand.model.ExpandResponse;
import com.fasterxml.jackson.core.JsonProcessingException;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * CLI entry point. Reads one expansion request as JSON and writes one JSON
 * response to stdout.
 *
 * <p>Usage:</p>
 * <pre>
 *   java -jar dst-rule-expander.jar                 # read request from stdin
 *   java -jar dst-rule-expander.jar request.json    # read request from file
 * </pre>
 *
 * <p>Exit codes: 0 success, 2 invalid request or policy ERROR hit,
 * 1 I/O or malformed JSON.</p>
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) {
        int code = run(args, System.in);
        if (code != 0) {
            System.exit(code);
        }
    }

    static int run(String[] args, InputStream stdin) {
        ExpandRequest request;
        try {
            request = readRequest(args, stdin);
        } catch (IOException e) {
            return fail(1, "Cannot read request: " + e.getMessage());
        }
        try {
            ExpandResponse response = new DstExpander().expand(request);
            System.out.println(Json.mapper().writeValueAsString(response));
            return 0;
        } catch (ExpansionException e) {
            return fail(2, e.getMessage());
        } catch (JsonProcessingException e) {
            return fail(1, "Cannot write response: " + e.getMessage());
        }
    }

    private static ExpandRequest readRequest(String[] args, InputStream stdin) throws IOException {
        try {
            if (args.length > 1) {
                throw new IOException("expected at most one argument (request JSON file)");
            }
            if (args.length == 1) {
                return Json.mapper().readValue(Files.readAllBytes(Path.of(args[0])), ExpandRequest.class);
            }
            return Json.mapper().readValue(stdin.readAllBytes(), ExpandRequest.class);
        } catch (JsonProcessingException e) {
            throw new IOException("malformed JSON: " + e.getOriginalMessage(), e);
        }
    }

    private static int fail(int code, String message) {
        try {
            System.out.println(Json.mapper().writeValueAsString(ExpandResponse.failure(message)));
        } catch (JsonProcessingException e) {
            System.out.println("{\"error\": \"json serialization failed\"}");
        }
        return code;
    }
}
