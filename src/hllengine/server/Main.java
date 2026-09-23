package hllengine.server;

import hllengine.api.ApiException;
import hllengine.hll.MurmurHash3;
import hllengine.json.Json;
import hllengine.json.JsonException;
import hllengine.test.TestRunner;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * JSON request entry point for the mergeable approximate-deduplication engine.
 *
 * <pre>
 *   java -jar hllengine.jar [--pretty]                 # one request on stdin
 *   java -jar hllengine.jar --file request.json [--pretty]
 *   java -jar hllengine.jar selftest
 *   java -jar hllengine.jar experiment [--trials N] [--out-dir DIR]
 * </pre>
 *
 * Output is always JSON (success: {@code {"ok":true,"result":...}},
 * failure: {@code {"ok":false,"error":{"code":...,"message":...}}}).
 * Exit code 0 on success, 2 on an API/format error, 3 on an internal failure.
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) {
        try {
            String mode = "request";
            String file = null;
            boolean pretty = false;
            int trials = 40;
            String outDir = "reports";

            for (int k = 0; k < args.length; k++) {
                switch (args[k]) {
                    case "--pretty": pretty = true; break;
                    case "--file": file = requireArg(args, ++k, "--file"); break;
                    case "selftest": mode = "selftest"; break;
                    case "experiment": mode = "experiment"; break;
                    case "--trials": trials = Integer.parseInt(requireArg(args, ++k, "--trials")); break;
                    case "--out-dir": outDir = requireArg(args, ++k, "--out-dir"); break;
                    case "--help", "-h":
                        printHelp();
                        return;
                    default:
                        throw new ApiException(ApiException.BAD_REQUEST, "unknown argument \"" + args[k] + "\"");
                }
            }

            switch (mode) {
                case "selftest": {
                    boolean ok = TestRunner.runAll(System.out);
                    System.exit(ok ? 0 : 1);
                }
                case "experiment": {
                    hllengine.test.AccuracyExperiment exp = new hllengine.test.AccuracyExperiment();
                    Map<String, Object> summary = exp.run(trials, Path.of(outDir));
                    System.out.println(pretty ? Json.writePretty(summary) : Json.write(summary));
                    return;
                }
                default: {
                    String text;
                    if (file != null) {
                        text = Files.readString(Path.of(file), StandardCharsets.UTF_8);
                    } else {
                        text = new String(System.in.readAllBytes(), StandardCharsets.UTF_8);
                    }
                    if (text.isBlank()) {
                        throw new ApiException(ApiException.BAD_FORMAT,
                                "empty input: provide a JSON request on stdin or with --file");
                    }
                    Map<String, Object> request;
                    try {
                        request = Json.parseObject(text);
                    } catch (JsonException je) {
                        throw new ApiException(ApiException.BAD_FORMAT, je.getMessage());
                    }
                    Engine engine = new Engine();
                    Map<String, Object> result = engine.handle(request);
                    Map<String, Object> envelope = new LinkedHashMap<>();
                    envelope.put("ok", true);
                    envelope.put("hashId", MurmurHash3.HASH_ID);
                    envelope.put("result", result);
                    System.out.println(pretty ? Json.writePretty(envelope) : Json.write(envelope));
                }
            }
        } catch (ApiException ae) {
            emitError(ae.code(), ae.getMessage(), 2);
        } catch (JsonException je) {
            emitError(ApiException.BAD_FORMAT, je.getMessage(), 2);
        } catch (Exception e) {
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("ok", false);
            err.put("error", Map.of(
                    "code", "INTERNAL_ERROR",
                    "message", String.valueOf(e.getMessage()),
                    "type", e.getClass().getSimpleName()));
            System.out.println(Json.write(err));
            System.exit(3);
        }
    }

    private static String requireArg(String[] args, int idx, String flag) {
        if (idx >= args.length) {
            throw new ApiException(ApiException.BAD_REQUEST, flag + " requires a value");
        }
        return args[idx];
    }

    private static void emitError(String code, String message, int exitCode) {
        Map<String, Object> envelope = new LinkedHashMap<>();
        envelope.put("ok", false);
        envelope.put("error", Map.of("code", code, "message", message));
        System.out.println(Json.write(envelope));
        System.exit(exitCode);
    }

    private static void printHelp() {
        System.out.println("""
                hllengine - mergeable approximate deduplication (HyperLogLog)

                Usage:
                  hllengine [--pretty]                     read one JSON request from stdin
                  hllengine --file <request.json> [--pretty]
                  hllengine selftest                       run the built-in test suite
                  hllengine experiment [--trials N] [--out-dir DIR]
                                                           fixed-seed accuracy experiment
                  hllengine --help

                All responses are JSON. Exit codes: 0 ok, 1 tests failed,
                2 bad request/format, 3 internal error.
                """);
    }
}
