package com.example.positiondiff.text;

import com.example.positiondiff.model.Eol;
import com.example.positiondiff.model.Line;

import java.util.ArrayList;
import java.util.List;

/**
 * Splits raw text into logical lines, keeping the exact newline form and the
 * presence/absence of a trailing newline as content.
 *
 * <p>Split rules:
 * <ul>
 *   <li>{@code "\r\n"} is one CRLF terminator (never split into CR + LF).</li>
 *   <li>{@code '\n'} is an LF terminator; a lone {@code '\r'} is ordinary
 *       text (classic-Mac CR-only files are not treated as line breaks).</li>
 *   <li>text after the final terminator is a line with {@link Eol#NONE}.</li>
 *   <li>empty input yields zero lines; {@code "\n"} yields one empty LF line;
 *       {@code "a"} yields one NONE line; {@code "a\n"} yields one LF line and
 *       is NOT the same sequence of lines as {@code "a"}.</li>
 * </ul>
 */
public final class LineSplitter {

    private LineSplitter() {}

    public static List<Line> split(String raw) {
        List<Line> lines = new ArrayList<>();
        int start = 0;
        int n = raw.length();
        for (int i = 0; i < n; i++) {
            char c = raw.charAt(i);
            if (c == '\n') {
                lines.add(new Line(raw.substring(start, i), Eol.LF));
                start = i + 1;
            } else if (c == '\r') {
                if (i + 1 < n && raw.charAt(i + 1) == '\n') {
                    lines.add(new Line(raw.substring(start, i), Eol.CRLF));
                    i++;
                    start = i + 1;
                }
                // lone CR: keep as ordinary text
            }
        }
        if (start < n) {
            lines.add(new Line(raw.substring(start), Eol.NONE));
        }
        return lines;
    }

    /** Exact inverse of {@link #split(String)} for a complete line list. */
    public static String join(List<Line> lines) {
        StringBuilder sb = new StringBuilder();
        for (Line line : lines) {
            sb.append(line.toRawString());
        }
        return sb.toString();
    }
}
