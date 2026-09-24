package com.example.diff.server;

import com.example.diff.DiffResult;
import com.example.diff.Edit;
import com.example.diff.Hunk;
import com.example.diff.HunkLine;
import com.example.diff.corpus.SearchEngine;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/** Serializes domain objects to JSON-compatible Maps. */
public final class Dto {

    private Dto() {
    }

    public static Map<String, Object> edit(Edit e) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("kind", e.kind.name().toLowerCase(Locale.ROOT));
        m.put("oldStart", e.oldStart);
        m.put("oldEnd", e.oldEnd);
        m.put("newStart", e.newStart);
        m.put("newEnd", e.newEnd);
        m.put("oldCharStart", e.oldCharStart);
        m.put("oldCharEnd", e.oldCharEnd);
        m.put("newCharStart", e.newCharStart);
        m.put("newCharEnd", e.newCharEnd);
        m.put("oldLines", e.oldLines);
        m.put("newLines", e.newLines);
        return m;
    }

    static Map<String, Object> hunkLine(HunkLine hl) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("kind", hl.kind.name().toLowerCase(Locale.ROOT));
        m.put("text", hl.text);
        m.put("oldLine", hl.oldLine);
        m.put("newLine", hl.newLine);
        return m;
    }

    static Map<String, Object> hunk(Hunk h) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("header", h.header());
        m.put("oldStart", h.oldStart);
        m.put("oldCount", h.oldCount);
        m.put("newStart", h.newStart);
        m.put("newCount", h.newCount);
        List<Object> lines = new ArrayList<>();
        for (HunkLine hl : h.lines) {
            lines.add(hunkLine(hl));
        }
        m.put("lines", lines);
        return m;
    }

    public static Map<String, Object> diffResult(DiffResult r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("degraded", r.degraded);
        m.put("shortest", r.shortest);
        m.put("degradeReason", r.degradeReason);
        m.put("oldLineCount", r.oldLineCount);
        m.put("newLineCount", r.newLineCount);
        m.put("deleteCount", r.deleteCount);
        m.put("insertCount", r.insertCount);
        m.put("editDistance", r.editDistance);
        m.put("maxEditDistance", r.maxEditDistance == Integer.MAX_VALUE ? null : r.maxEditDistance);
        m.put("maxInputChars", r.maxInputChars);
        List<Object> edits = new ArrayList<>();
        for (Edit e : r.edits) {
            edits.add(edit(e));
        }
        m.put("edits", edits);
        List<Object> hunks = new ArrayList<>();
        for (Hunk h : r.hunks) {
            hunks.add(hunk(h));
        }
        m.put("hunks", hunks);
        return m;
    }

    public static Map<String, Object> hit(SearchEngine.Hit h) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("docId", h.docId);
        m.put("title", h.title);
        m.put("score", h.score);
        m.put("matchedLines", h.matchedLines);
        m.put("matchedOffsets", h.matchedOffsets);
        m.put("snippets", h.snippets);
        return m;
    }
}
