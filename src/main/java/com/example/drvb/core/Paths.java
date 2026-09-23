package com.example.drvb.core;

import java.util.List;
import java.util.Map;

/** Dotted-path resolution over nested {@code Map}/{@code List} payloads. */
final class Paths {

    private Paths() {
    }

    /**
     * Resolves a dotted path such as {@code user.tier} or {@code items.0.id}.
     * Numeric segments index lists; missing segments return {@code null}.
     */
    static Object resolve(Map<String, Object> root, String path) {
        Object cur = root;
        for (String seg : split(path)) {
            if (cur instanceof Map<?, ?> map) {
                cur = map.get(seg);
            } else if (cur instanceof List<?> list) {
                Integer idx = index(seg);
                if (idx == null || idx < 0 || idx >= list.size()) {
                    return null;
                }
                cur = list.get(idx);
            } else {
                return null;
            }
            if (cur == null) {
                return null;
            }
        }
        return cur;
    }

    private static String[] split(String path) {
        // A leading dot would denote a literal key starting with '.'; rule
        // authors do not need that, so plain split is sufficient.
        return path.split("\\.", -1);
    }

    private static Integer index(String seg) {
        if (seg.isEmpty()) {
            return null;
        }
        for (int i = 0; i < seg.length(); i++) {
            if (!Character.isDigit(seg.charAt(i))) {
                return null;
            }
        }
        try {
            return Integer.valueOf(seg);
        } catch (NumberFormatException e) {
            return null;
        }
    }
}
