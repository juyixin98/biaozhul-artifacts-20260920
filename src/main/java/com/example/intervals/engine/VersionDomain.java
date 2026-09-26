package com.example.intervals.engine;

import com.example.intervals.error.ErrorCode;
import com.example.intervals.error.IntervalException;

/**
 * The version domain.
 *
 * <p>Endpoint tokens are arbitrary non-empty strings ordered by fixed
 * lexicographic order (Java UTF-16 {@link String#compareTo}, i.e. Unicode code
 * unit order). This deliberately does <strong>not</strong> attempt semantic
 * version parsing: rule sets that need {@code v10 > v9} should choose tokens
 * whose lexical order already matches the intended order (e.g. zero-padded
 * {@code v09}, {@code v10}). Lexicographic order is total, fixed, and gives a
 * unique normalized result, which is what the algebra requires.
 */
public final class VersionDomain implements Domain<String> {

    public static final String ID = "version";

    @Override
    public String id() {
        return ID;
    }

    @Override
    public String description() {
        return "arbitrary version strings ordered by fixed lexicographic (Unicode) order";
    }

    @Override
    public String parseEndpoint(String raw) {
        if (raw == null || raw.isEmpty()) {
            throw new IntervalException(ErrorCode.INVALID_ENDPOINT,
                    "version endpoint must be a non-empty string");
        }
        return raw;
    }

    @Override
    public String formatEndpoint(String value) {
        return value;
    }
}
