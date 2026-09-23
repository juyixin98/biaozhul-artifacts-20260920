package com.example.uninorm;

/**
 * Half-open offset range of a match in the ORIGINAL text.
 *
 * <p>All three coordinate systems describe the same logical span
 * {@code [start, end)} so callers can pick the coordinate system their
 * downstream consumer uses:
 * <ul>
 *   <li>{@code startUtf16/endUtf16}   - Java {@code String.charAt} indices
 *       (one {@code char}; surrogate pairs count as 2),</li>
 *   <li>{@code startCodePoint/endCodePoint} - Unicode code-point indices,</li>
 *   <li>{@code startUtf8/endUtf8}     - byte offsets of the UTF-8 encoding.</li>
 * </ul>
 *
 * <p>Invariant guaranteed by {@link TextNormalizer}: UTF-16 and UTF-8
 * boundaries always fall on code-point boundaries - a range never slices a
 * surrogate pair or a multi-byte sequence in half.
 */
public record OffsetRange(
        int startUtf16,
        int endUtf16,
        int startCodePoint,
        int endCodePoint,
        int startUtf8,
        int endUtf8) {
}
