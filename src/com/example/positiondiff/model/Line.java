package com.example.positiondiff.model;

import java.util.Objects;

/**
 * One logical line: visible text WITHOUT any line terminator, plus the
 * terminator kind attached to it. "a\n" is one line ("a", LF). A trailing
 * newline is therefore encoded structurally ("b", LF) and is distinct from
 * "b" without newline ("b", NONE).
 */
public final class Line {
    private final String text;
    private final Eol eol;

    public Line(String text, Eol eol) {
        this.text = text;
        this.eol = Objects.requireNonNull(eol);
    }

    public String text() {
        return text;
    }

    public Eol eol() {
        return eol;
    }

    public String toRawString() {
        return text + eol.separator();
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (!(o instanceof Line other)) return false;
        return text.equals(other.text) && eol == other.eol;
    }

    @Override
    public int hashCode() {
        return Objects.hash(text, eol);
    }

    @Override
    public String toString() {
        return "Line[" + text + ", " + eol + "]";
    }
}
