package com.example.diff;

import java.util.List;

/**
 * Applies an edit script produced by {@link MyersDiff} (or supplied by a
 * client) to the old text and verifies that the script is internally
 * consistent before touching anything.
 *
 * <p>Validation rules:
 * <ul>
 *   <li>edits are ordered, adjacent, non-overlapping in both coordinate systems</li>
 *   <li>DELETE/INSERT/EQUAL ranges are well-formed</li>
 *   <li>line texts carried by the edit match the old/new reference texts
 *       (when a new reference is supplied; the new text is normally what we
 *       are reconstructing, so line-material checks use the old text and,
 *       for inserts, the carried newLines themselves)</li>
 * </ul>
 */
public final class ApplyEdits {

    public static final class Result {
        public final boolean ok;
        public final String text;    // reconstructed text (empty on failure)
        public final String error;   // null when ok

        Result(boolean ok, String text, String error) {
            this.ok = ok;
            this.text = text;
            this.error = error;
        }
    }

    private ApplyEdits() {
    }

    /** Rebuild the new text from edits against the old text. */
    public static Result apply(String oldText, List<Edit> edits) {
        List<Lines.Line> oldLines = Lines.split(oldText);
        String error = validate(oldLines, edits);
        if (error != null) {
            return new Result(false, "", error);
        }
        StringBuilder sb = new StringBuilder();
        for (Edit e : edits) {
            switch (e.kind) {
                case EQUAL:
                    for (String t : e.oldLines) {
                        sb.append(t);
                    }
                    break;
                case DELETE:
                    break;
                case INSERT:
                    for (String t : e.newLines) {
                        sb.append(t);
                    }
                    break;
            }
        }
        return new Result(true, sb.toString(), null);
    }

    /**
     * Full structural validation. Exposes the first problem found, which makes
     * malformed client-supplied scripts fail loudly instead of producing
     * garbage.
     */
    public static String validate(List<Lines.Line> oldLines, List<Edit> edits) {
        int expOld = 0;
        int expNew = 0;
        int n = oldLines.size();
        int mHint = -1;
        for (int idx = 0; idx < edits.size(); idx++) {
            Edit e = edits.get(idx);
            if (e == null) {
                return "edit at index " + idx + " is null";
            }
            if (e.oldStart != expOld) {
                return "edit " + idx + " oldStart " + e.oldStart + " != expected " + expOld;
            }
            if (e.newStart != expNew) {
                return "edit " + idx + " newStart " + e.newStart + " != expected " + expNew;
            }
            if (e.oldEnd < e.oldStart || e.newEnd < e.newStart) {
                return "edit " + idx + " has inverted range";
            }
            switch (e.kind) {
                case EQUAL:
                    if (e.oldCount() != e.newCount()) {
                        return "EQUAL edit " + idx + " spans different old/new counts";
                    }
                    if (e.oldEnd > n) {
                        return "EQUAL edit " + idx + " runs past old text";
                    }
                    if (e.oldLines.size() != e.oldCount() || e.newLines.size() != e.newCount()) {
                        return "EQUAL edit " + idx + " line material length mismatch";
                    }
                    for (int t = 0; t < e.oldCount(); t++) {
                        String actual = oldLines.get(e.oldStart + t).text;
                        if (!actual.equals(e.oldLines.get(t))) {
                            return "EQUAL edit " + idx + " old line material mismatch at line " + (e.oldStart + t);
                        }
                        if (!actual.equals(e.newLines.get(t))) {
                            return "EQUAL edit " + idx + " new line material mismatch at line " + (e.newStart + t);
                        }
                    }
                    break;
                case DELETE:
                    if (e.newStart != e.newEnd) {
                        return "DELETE edit " + idx + " must have zero new range";
                    }
                    if (e.oldEnd > n) {
                        return "DELETE edit " + idx + " runs past old text";
                    }
                    if (e.oldLines.size() != e.oldCount() || !e.newLines.isEmpty()) {
                        return "DELETE edit " + idx + " line material mismatch";
                    }
                    for (int t = 0; t < e.oldCount(); t++) {
                        if (!oldLines.get(e.oldStart + t).text.equals(e.oldLines.get(t))) {
                            return "DELETE edit " + idx + " old line material mismatch at line " + (e.oldStart + t);
                        }
                    }
                    break;
                case INSERT:
                    if (e.oldStart != e.oldEnd) {
                        return "INSERT edit " + idx + " must have zero old range";
                    }
                    if (e.oldStart > n) {
                        return "INSERT edit " + idx + " old boundary out of range";
                    }
                    if (e.newLines.size() != e.newCount() || !e.oldLines.isEmpty()) {
                        return "INSERT edit " + idx + " line material mismatch";
                    }
                    break;
            }
            expOld = e.oldEnd;
            expNew = e.newEnd;
        }
        if (expOld != n) {
            return "script covers " + expOld + " old lines but text has " + n;
        }
        return null;
    }
}
