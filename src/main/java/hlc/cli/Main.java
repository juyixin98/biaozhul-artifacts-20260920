package hlc.cli;

import hlc.HLCException;
import hlc.HLCFileStore;
import hlc.Json;
import hlc.TimeZoneInfo;
import hlc.server.ApiService;
import hlc.server.ScenarioService;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Command-line interface for local experimentation and for the fixed-data test fixtures.
 *
 * <pre>
 *   hlc info
 *   hlc nodes create --node alice [--initial-pt MICROS] [--state FILE]
 *   hlc tick    --node alice --pt 1000 [--state FILE]
 *   hlc send    --node alice --pt 2000 [--state FILE]
 *   hlc receive --node bob   --pt 2500 --message 2000:1 [--state FILE]
 *   hlc snapshot --node bob [--state FILE]
 *   hlc simulate scenario.json
 *   hlc demo
 * </pre>
 *
 * Mutating subcommands load prior node state from the state file, apply the operation, and
 * persist again, so a sequence of shell invocations behaves like one durable process.
 */
public final class Main {

    private static final String DEFAULT_STATE = "data/cli-state.properties";

    private Main() {
    }

    public static void main(String[] argv) {
        try {
            Map<String, Object> out = run(argv);
            System.out.println(Json.writePretty(out));
        } catch (HLCException e) {
            System.err.println(Json.writePretty(Map.of("success", false, "error", e.getMessage())));
            System.exit(2);
        } catch (Exception e) {
            System.err.println(Json.writePretty(Map.of("success", false,
                    "error", e.getClass().getSimpleName() + ": " + e.getMessage())));
            System.exit(1);
        }
    }

    static Map<String, Object> run(String[] argv) throws Exception {
        if (argv.length == 0) {
            throw new HLCException("no subcommand given; try: info, nodes, tick, send, receive, "
                    + "snapshot, simulate, demo");
        }
        String cmd = argv[0];
        Args args = Args.parse(argv, 1);
        return switch (cmd) {
            case "info" -> Map.of("tzdb", TimeZoneInfo.version(),
                    "defaultZone", TimeZoneInfo.defaultZone(),
                    "timestampFormat", "<l-micros>:<counter>");
            case "nodes" -> nodes(args);
            case "tick" -> mutate(args, svc -> svc.local(clockReq(args)));
            case "send" -> mutate(args, svc -> svc.send(clockReq(args)));
            case "receive" -> mutate(args, svc ->
                    svc.receive(receiveReq(args)));
            case "snapshot" -> withLoaded(args, svc ->
                    svc.snapshot(Map.of("node", args.required("node"))));
            case "simulate" -> simulate(args);
            case "demo" -> DemoScenarios.runAll();
            default -> throw new HLCException("unknown subcommand: " + cmd);
        };
    }

    private static Map<String, Object> nodes(Args args) {
        if (!"create".equals(args.required("__pos1"))) {
            throw new HLCException("usage: nodes create --node NAME [--initial-pt MICROS]");
        }
        return mutate(args, svc -> {
            Map<String, Object> req = new LinkedHashMap<>();
            req.put("node", args.required("node"));
            String pt = args.option("initial-pt", null);
            if (pt != null) {
                req.put("initialPhysicalMicros", parseMicros(pt));
            }
            return svc.createNode(req);
        });
    }

    private static Map<String, Object> simulate(Args args) throws Exception {
        String file = args.required("__pos1");
        String text = Files.readString(Path.of(file), StandardCharsets.UTF_8);
        return ScenarioService.simulate(Json.parseObject(text));
    }

    private static Map<String, Object> clockReq(Args args) {
        Map<String, Object> req = new LinkedHashMap<>();
        req.put("node", args.required("node"));
        req.put("physicalMicros", parseMicros(args.required("pt")));
        return req;
    }

    private static Map<String, Object> receiveReq(Args args) {
        Map<String, Object> req = clockReq(args);
        req.put("message", args.required("message"));
        return req;
    }

    private static long parseMicros(String s) {
        try {
            long v = Long.parseLong(s);
            if (v < 0) {
                throw new NumberFormatException();
            }
            return v;
        } catch (NumberFormatException e) {
            throw new HLCException("expected a non-negative integer of microseconds, got: " + s);
        }
    }

    /** Read-only use of a freshly loaded service. */
    private static Map<String, Object> withLoaded(Args args, ServiceAction action) {
        ApiService svc = new ApiService(new HLCFileStore(Path.of(args.option("state", DEFAULT_STATE))));
        svc.load();
        return action.run(svc);
    }

    /** Load, apply one mutation, then atomically persist. */
    private static Map<String, Object> mutate(Args args, ServiceAction action) {
        return withLoaded(args, svc -> {
            Map<String, Object> result = action.run(svc);
            svc.save();
            return result;
        });
    }

    @FunctionalInterface
    interface ServiceAction {
        Map<String, Object> run(ApiService svc);
    }

    /** Tiny {@code --key value} / positional argument reader. */
    static final class Args {
        private final Map<String, String> opts = new LinkedHashMap<>();
        private int positional;

        static Args parse(String[] argv, int from) {
            Args a = new Args();
            for (int i = from; i < argv.length; i++) {
                String t = argv[i];
                if (t.startsWith("--")) {
                    String key = t.substring(2);
                    if (i + 1 >= argv.length) {
                        throw new HLCException("option --" + key + " requires a value");
                    }
                    a.opts.put(key, argv[++i]);
                } else {
                    a.opts.put("__pos" + (++a.positional), t);
                }
            }
            return a;
        }

        String required(String key) {
            String v = opts.get(key);
            if (v == null) {
                throw new HLCException("missing required option --" + key);
            }
            return v;
        }

        String option(String key, String dflt) {
            return opts.getOrDefault(key, dflt);
        }
    }
}
