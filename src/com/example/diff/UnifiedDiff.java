package com.example.diff;

import java.util.List;

/** Renders hunks as unified-diff text. */
public final class UnifiedDiff {

    private UnifiedDiff() {
    }

    public static String render(DiffResult r) {
        return render(r.hunks);
    }

    public static String render(List<Hunk> hunks) {
        StringBuilder sb = new StringBuilder();
        for (Hunk h : hunks) {
            sb.append(h.header()).append('\n');
            for (HunkLine hl : h.lines) {
                switch (hl.kind) {
                    case CONTEXT:
                        sb.append(' ');
                        break;
                    case DELETED:
                        sb.append('-');
                        break;
                    case ADDED:
                        sb.append('+');
                        break;
                }
                sb.append(hl.text);
                if (hl.text.isEmpty() || !hl.text.endsWith("\n")) {
                    // The marker goes on its OWN line, standard diff convention
                    // (the content line above is emitted without a terminator).
                    sb.append('\n');
                    sb.append("\\ No newline at end of file\n");
                }
            }
        }
        return sb.toString();
    }
}
