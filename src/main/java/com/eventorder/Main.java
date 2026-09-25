package com.eventorder;

import com.eventorder.engine.OrderEngine;
import com.eventorder.io.JsonCodec;
import com.eventorder.model.OrderRequest;
import com.eventorder.model.OrderResult;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * CLI entry point. Reads a request JSON document from a file argument or stdin,
 * writes the result JSON to stdout.
 *
 * Exit codes: 0 = computed (OK or UNSATISFIABLE), 1 = unreadable/invalid request.
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) {
        JsonCodec codec = new JsonCodec();
        String input;
        try {
            input = readInput(args);
        } catch (IOException e) {
            System.err.println("error: cannot read input: " + e.getMessage());
            System.exit(1);
            return;
        }

        OrderRequest request;
        try {
            request = codec.readRequest(input);
        } catch (IOException e) {
            System.err.println("error: malformed request JSON: " + e.getMessage());
            System.exit(1);
            return;
        }

        OrderResult result = OrderEngine.solve(request);
        try {
            System.out.println(codec.writeResult(result));
        } catch (IOException e) {
            System.err.println("error: cannot serialize result: " + e.getMessage());
            System.exit(1);
            return;
        }
        if ("INVALID_INPUT".equals(result.status())) {
            System.exit(1);
        }
    }

    private static String readInput(String[] args) throws IOException {
        if (args.length > 0) {
            return Files.readString(Path.of(args[0]), StandardCharsets.UTF_8);
        }
        return new String(System.in.readAllBytes(), StandardCharsets.UTF_8);
    }
}
