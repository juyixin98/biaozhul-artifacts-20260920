package com.example.diff;

import java.util.ArrayList;
import java.util.List;

/**
 * Splits text into line tokens WITHOUT discarding line terminators.
 *
 * <p>Newline form and the presence of a trailing newline are content:
 * <ul>
 *   <li>{@code "a\nb"}  &rarr; {@code ["a\n", "b"]}  (no trailing newline)</li>
 *   <li>{@code "a\nb\n"} &rarr; {@code ["a\n", "b\n"]} (trailing newline)</li>
 *   <li>{@code "a\r\nb"} &rarr; {@code ["a\r\n", "b"]} (CRLF kept)</li>
 *   <li>{@code ""}       &rarr; {@code []}             (empty file)</li>
 * </ul>
 * A lone CR is not treated as a line terminator (it stays inside the line
 * content), matching common diff tools' treatment of CR.
 *
 * <p>Each token also records the character offset where it begins in the
 * original text, so every reported edit can carry exact positions.
 */
public final class Lines {

    /** One line: its full text including terminator, and its char offset. */
    public static final class Line {
        public final String text;
        public final int offset;

        public Line(String text, int offset) {
            this.text = text;
            this.offset = offset;
        }

        /** Line text without the terminator ("\n" or "\r\n"). */
        public String content() {
            if (text.endsWith("\r\n")) {
                return text.substring(0, text.length() - 2);
            }
            if (text.endsWith("\n")) {
                return text.substring(0, text.length() - 1);
            }
            return text;
        }

        /** Terminator suffix of this line: "\r\n", "\n", or "" for the last unterminated line. */
        public String ending() {
            if (text.endsWith("\r\n")) {
                return "\r\n";
            }
            if (text.endsWith("\n")) {
                return "\n";
            }
            return "";
        }
    }

    private Lines() {
    }

    public static List<Line> split(String s) {
        List<Line> out = new ArrayList<>();
        int start = 0;
        int n = s.length();
        for (int i = 0; i < n; i++) {
            char c = s.charAt(i);
            if (c == '\n') {
                int end = i + 1;
                out.add(new Line(s.substring(start, end), start));
                start = end;
            }
        }
        if (start < n) {
            out.add(new Line(s.substring(start), start));
        }
        return out;
    }

    /** Convenience: just the line texts. */
    public static List<String> texts(List<Line> lines) {
        List<String> out = new ArrayList<>(lines.size());
        for (Line l : lines) {
            out.add(l.text);
        }
        return out;
    }
}
