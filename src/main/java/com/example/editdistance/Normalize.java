package com.example.editdistance;

import java.text.Normalizer;

/**
 * Unicode normalization policy applied consistently to corpus terms (at index build
 * time) and to queries (at search time).
 *
 * <p>Default is {@link #NFC}: canonically equivalent strings such as "é" written as
 * one code point (U+00E9) versus "e" + combining acute (U+0065 U+0301) normalize to
 * the same code-point sequence and therefore have distance 0. With {@link #NONE}
 * they have distance 1. The policy is a server-level setting so that index and query
 * sides can never disagree.
 */
public enum Normalize {
    NONE,
    NFC,
    NFD,
    NFKC,
    NFKD;

    public String apply(String s) {
        if (this == NONE) {
            return s;
        }
        return Normalizer.normalize(s, Normalizer.Form.valueOf(name()));
    }
}
