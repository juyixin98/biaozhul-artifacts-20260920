package io.example.orderedcommit;

import java.util.LinkedHashMap;
import java.util.Map;

/** Tiny {@code --key value} argument parser (no value-less boolean flags needed). */
final class Args {

    private Args() {
    }

    static Map<String, String> parse(String[] args) {
        Map<String, String> out = new LinkedHashMap<>();
        for (int i = 0; i < args.length; i++) {
            String a = args[i];
            if (!a.startsWith("--") || a.length() == 2) {
                throw new IllegalArgumentException("unexpected argument: " + a);
            }
            String key = a.substring(2);
            if (i + 1 >= args.length) {
                throw new IllegalArgumentException("missing value for --" + key);
            }
            out.put(key, args[++i]);
        }
        return out;
    }

    static int integer(Map<String, String> opts, String key, int fallback) {
        String v = opts.get(key);
        if (v == null) {
            return fallback;
        }
        try {
            return Integer.parseInt(v);
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException("--" + key + " must be an integer: " + v);
        }
    }

    static long longInteger(Map<String, String> opts, String key, long fallback) {
        String v = opts.get(key);
        if (v == null) {
            return fallback;
        }
        try {
            return Long.parseLong(v);
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException("--" + key + " must be a long integer: " + v);
        }
    }
}
