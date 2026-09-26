package com.example.eventorder;

import com.example.eventorder.engine.OrderEngine;
import com.example.eventorder.engine.RequestException;
import com.example.eventorder.engine.TzdbVersion;
import com.example.eventorder.json.JsonIO;
import com.example.eventorder.model.OrderRequest;
import com.example.eventorder.model.OrderResponse;
import java.io.IOException;
import java.io.InputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * Command-line entry point.
 *
 * <p>Usage: {@code java -jar event-order-reconstruction.jar [request.json]}
 * With no argument the request is read from standard input. The response JSON
 * is written to standard output.
 *
 * <p>Exit codes: 0 processed (including an unsatisfiable request),
 * 2 invalid request (an error envelope is still printed), 1 I/O/tool failure.
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) {
        System.exit(run(args, System.in, System.out, System.err));
    }

    /** Test-friendly core: returns the process exit code without terminating the JVM. */
    static int run(String[] args, InputStream in, PrintStream out, PrintStream err) {
        JsonIO jsonIO = new JsonIO();
        String tzdb = TzdbVersion.detect();
        try {
            String raw = readInput(args, in);
            OrderRequest request = jsonIO.readRequest(raw);
            OrderResponse response = new OrderEngine(tzdb).process(request);
            out.println(jsonIO.writeResponse(response));
            return 0;
        } catch (RequestException e) {
            out.println("{\"error\":\"" + escape(e.getMessage()) + "\""
                    + ",\"tzdbVersion\":\"" + escape(tzdb) + "\"}");
            return 2;
        } catch (IOException e) {
            err.println("I/O error: " + e.getMessage());
            return 1;
        }
    }

    private static String readInput(String[] args, InputStream in) throws IOException {
        if (args.length > 1) {
            throw new IOException("expected at most one file argument; got " + args.length);
        }
        if (args.length == 1) {
            return Files.readString(Path.of(args[0]), StandardCharsets.UTF_8);
        }
        return new String(in.readAllBytes(), StandardCharsets.UTF_8);
    }

    private static String escape(String text) {
        if (text == null) {
            return "";
        }
        return text.replace("\\", "\\\\")
                .replace("\"", "\\\"")
                .replace("\n", "\\n")
                .replace("\r", "\\r")
                .replace("\t", "\\t");
    }
}
