package partjoin;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * JSON request entry point (pure backend, no HTTP server).
 *
 * Usage:
 *   java partjoin.Main [plan] request.json [response.json]
 *   cat request.json | java partjoin.Main - [response.json]
 *
 * With the leading "plan" argument the execution plan is produced without
 * running the join. Exit code 0 = success, 2 = join/request error
 * (an {"ok":false,"error":{...}} document is emitted), 1 = usage/IO failure.
 */
public final class Main {

    public static void main(String[] args) {
        boolean planOnly = false;
        int first = 0;
        if (args.length > 0 && args[0].equalsIgnoreCase("plan")) {
            planOnly = true;
            first = 1;
        }
        if (args.length - first < 1 || args.length - first > 2) {
            System.err.println("Usage: partjoin [plan] <request.json|-> [response.json]");
            System.exit(1);
        }
        String reqArg = args[first];
        String respArg = args.length - first == 2 ? args[first + 1] : null;

        String requestText;
        try {
            if ("-".equals(reqArg)) {
                requestText = new String(System.in.readAllBytes(), StandardCharsets.UTF_8);
            } else {
                requestText = Files.readString(Path.of(reqArg), StandardCharsets.UTF_8);
            }
        } catch (Exception e) {
            System.err.println("Cannot read request: " + e.getMessage());
            System.exit(1);
            return;
        }

        try {
            Map<String, Object> req = Json.parseObject(requestText);
            Object result = planOnly ? RequestRunner.plan(req) : RequestRunner.run(req);
            String responseText;
            if (planOnly) {
                Map<String, Object> wrapper = new LinkedHashMap<>();
                wrapper.put("ok", true);
                wrapper.put("plan", result);
                responseText = Json.writePretty(wrapper);
            } else {
                responseText = Json.writePretty(result);
            }
            if (respArg != null) {
                Path p = Path.of(respArg);
                try {
                    if (p.getParent() != null) Files.createDirectories(p.getParent());
                    Files.writeString(p, responseText, StandardCharsets.UTF_8);
                } catch (java.io.IOException e) {
                    System.err.println("Cannot write response file: " + e.getMessage());
                    System.exit(1);
                }
                System.out.println("Response written to " + p.toAbsolutePath());
            } else {
                System.out.println(responseText);
            }
        } catch (JoinException e) {
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("ok", false);
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("code", e.code());
            body.put("message", e.getMessage());
            err.put("error", body);
            String text = Json.writePretty(err);
            if (respArg != null) {
                try {
                    Files.writeString(Path.of(respArg), text, StandardCharsets.UTF_8);
                } catch (Exception ioe) {
                    System.err.println(text);
                }
            } else {
                System.err.println(text);
            }
            System.exit(2);
        }
    }
}
