package com.example.positiondiff.model;

/**
 * Line ending kind. Treated as part of the line's content: two lines with the
 * same visible text but different endings (LF vs CRLF vs no newline at EOF)
 * are NOT equal for diffing purposes.
 */
public enum Eol {
    LF("\n"),
    CRLF("\r\n"),
    /** Last logical line of a file that does not end with a newline. */
    NONE("");

    private final String separator;

    Eol(String separator) {
        this.separator = separator;
    }

    public String separator() {
        return separator;
    }
}
