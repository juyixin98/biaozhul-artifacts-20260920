package com.example.diff;

import java.util.ArrayList;
import java.util.List;

/**
 * Fixed-case tests for Myers diff, the edit script, and application.
 * Covers: all-identical lines, empty files on each side, CRLF vs LF,
 * trailing-newline differences, insert/delete/replace, hunks and the
 * no-newline marker.
 */
public final class MyersDiffTests {

    private MyersDiffTests() {
    }

    public static void register(TestFramework tf) {
        tf.test("identical texts -> zero edits, shortest", MyersDiffTests::identical);
        tf.test("all repeated identical lines -> zero edits", MyersDiffTests::allRepeated);
        tf.test("empty -> empty", MyersDiffTests::emptyBoth);
        tf.test("empty -> nonempty: pure insert", MyersDiffTests::emptyToNonEmpty);
        tf.test("nonempty -> empty: pure delete", MyersDiffTests::nonEmptyToEmpty);
        tf.test("insert one line in the middle", MyersDiffTests::insertMiddle);
        tf.test("delete one line in the middle", MyersDiffTests::deleteMiddle);
        tf.test("replace one line", MyersDiffTests::replaceOne);
        tf.test("CRLF preserved as content across diff", MyersDiffTests::crlfRoundTrip);
        tf.test("LF text vs CRLF text treats endings as changed", MyersDiffTests::lfVsCrlf);
        tf.test("adding a trailing newline is a real change", MyersDiffTests::addTrailingNewline);
        tf.test("removing a trailing newline is a real change", MyersDiffTests::removeTrailingNewline);
        tf.test("CRLF same content vs LF: endings aligned at last line", MyersDiffTests::crlfVsLfOnlyEnding);
        tf.test("char offsets are exact within text", MyersDiffTests::charOffsets);
        tf.test("hunk header and context lines", MyersDiffTests::hunkContext);
        tf.test("unified diff no-newline marker", MyersDiffTests::noNewlineMarker);
        tf.test("minimality on constructed case with repeated lines", MyersDiffTests::minimalWithRepeats);
        tf.test("edits coalesce blocks of consecutive same-kind changes", MyersDiffTests::coalesced);
        tf.test("budget above true distance still returns shortest", MyersDiffTests::budgetSufficient);
        tf.test("budget zero on identical text is not degraded", MyersDiffTests::budgetZeroIdentical);
    }

    private static DiffResult d(String a, String b) {
        return new MyersDiff().diff(a, b);
    }

    private static void assertRoundTrip(String a, String b) {
        DiffResult r = d(a, b);
        ApplyEdits.Result ar = ApplyEdits.apply(a, r.edits);
        TestFramework.assertTrue(ar.ok, "apply failed: " + ar.error);
        TestFramework.assertEquals(b, ar.text);
    }

    private static void identical() {
        String t = "a\nb\nc\n";
        DiffResult r = d(t, t);
        TestFramework.assertEquals(0, r.editDistance);
        TestFramework.assertEquals(false, r.degraded);
        TestFramework.assertEquals(true, r.shortest);
        TestFramework.assertEquals(1, r.edits.size());
        TestFramework.assertEquals(Edit.Kind.EQUAL, r.edits.get(0).kind);
    }

    private static void allRepeated() {
        String t = "same\nsame\nsame\nsame\n";
        DiffResult r = d(t, t);
        TestFramework.assertEquals(0, r.editDistance);
        assertRoundTrip(t, t);

        // Repeated lines with a small perturbation must find a 2-edit script.
        String t2 = "same\nsame\nDIFF\nsame\nsame\n";
        DiffResult r2 = d(t, t2);
        // old has 4 same, new has 4 same + 1 diff. shortest = 1 insert.
        TestFramework.assertEquals(1, r2.editDistance, "expected a single insertion");
        assertRoundTrip(t, t2);
    }

    private static void emptyBoth() {
        DiffResult r = d("", "");
        TestFramework.assertEquals(0, r.editDistance);
        TestFramework.assertEquals(0, r.edits.size());
        assertRoundTrip("", "");
    }

    private static void emptyToNonEmpty() {
        String b = "x\ny\n";
        DiffResult r = d("", b);
        TestFramework.assertEquals(2, r.insertCount);
        TestFramework.assertEquals(0, r.deleteCount);
        TestFramework.assertEquals(Edit.Kind.INSERT, r.edits.get(0).kind);
        assertRoundTrip("", b);
        TestFramework.assertEquals("@@ -0,0 +1,2 @@", r.hunks.get(0).header());
    }

    private static void nonEmptyToEmpty() {
        String a = "x\ny\n";
        DiffResult r = d(a, "");
        TestFramework.assertEquals(2, r.deleteCount);
        TestFramework.assertEquals(0, r.insertCount);
        TestFramework.assertEquals(Edit.Kind.DELETE, r.edits.get(0).kind);
        assertRoundTrip(a, "");
        TestFramework.assertEquals("@@ -1,2 +0,0 @@", r.hunks.get(0).header());
    }

    private static void insertMiddle() {
        String a = "1\n2\n4\n";
        String b = "1\n2\n3\n4\n";
        DiffResult r = d(a, b);
        TestFramework.assertEquals(1, r.editDistance);
        assertRoundTrip(a, b);
    }

    private static void deleteMiddle() {
        String a = "1\n2\n3\n4\n";
        String b = "1\n2\n4\n";
        DiffResult r = d(a, b);
        TestFramework.assertEquals(1, r.editDistance);
        assertRoundTrip(a, b);
    }

    private static void replaceOne() {
        String a = "a\nb\nc\n";
        String b = "a\nB\nc\n";
        DiffResult r = d(a, b);
        TestFramework.assertEquals(2, r.editDistance, "replace = 1 delete + 1 insert");
        assertRoundTrip(a, b);
    }

    private static void crlfRoundTrip() {
        String a = "line one\r\nline two\r\n";
        String b = "line one\r\nchanged\r\nline two\r\n";
        DiffResult r = d(a, b);
        TestFramework.assertEquals(1, r.insertCount);
        assertRoundTrip(a, b);
        // No line token should lose its \r.
        for (Edit e : r.edits) {
            for (String l : e.newLines) {
                if (!l.isEmpty()) {
                    TestFramework.assertTrue(l.endsWith("\r\n"), "new line should keep CRLF: <" + l + ">");
                }
            }
        }
    }

    private static void lfVsCrlf() {
        String a = "a\nb\n";
        String b = "a\r\nb\r\n";
        DiffResult r = d(a, b);
        // Line contents match but terminators differ: every line is changed at
        // token level (2 deletes + 2 inserts). This intentionally treats
        // newline form as content.
        TestFramework.assertEquals(4, r.editDistance,
                "terminator difference is a content difference");
        assertRoundTrip(a, b);
    }

    private static void crlfVsLfOnlyEnding() {
        // Same body, only the terminator changes on both lines.
        assertRoundTrip("a\r\nb", "a\nb");
        assertRoundTrip("a\r\nb\r\n", "a\nb\n");
    }

    private static void addTrailingNewline() {
        String a = "a\nb";
        String b = "a\nb\n";
        DiffResult r = d(a, b);
        // "b" -> "b\n": delete old last line, insert terminated one => 2 edits.
        TestFramework.assertEquals(2, r.editDistance);
        assertRoundTrip(a, b);
    }

    private static void removeTrailingNewline() {
        String a = "a\nb\n";
        String b = "a\nb";
        DiffResult r = d(a, b);
        TestFramework.assertEquals(2, r.editDistance);
        assertRoundTrip(a, b);
    }

    private static void charOffsets() {
        String a = "aa\nbb\n"; // offsets: line0 [0,3), line1 [3,6)
        String b = "aa\nXX\n";
        DiffResult r = d(a, b);
        Edit del = null;
        for (Edit e : r.edits) {
            if (e.kind == Edit.Kind.DELETE) {
                del = e;
            }
        }
        TestFramework.assertTrue(del != null, "expected a delete edit");
        TestFramework.assertEquals(3, del.oldCharStart);
        TestFramework.assertEquals(6, del.oldCharEnd);
        TestFramework.assertEquals("bb\n", a.substring(del.oldCharStart, del.oldCharEnd));
    }

    private static void hunkContext() {
        String a = "0\n1\n2\n3\n4\n5\n6\n7\n8\n9\n";
        String b = "0\n1\n2\nX\n4\n5\n6\n7\n8\n9\n";
        DiffResult r = new MyersDiff(MyersDiff.UNLIMITED_DISTANCE, MyersDiff.DEFAULT_MAX_INPUT_CHARS, 2).diff(a, b);
        TestFramework.assertEquals(1, r.hunks.size());
        Hunk h = r.hunks.get(0);
        // 2 context lines around changed line 4 (1-based): range 2..6 => 5 lines old side
        TestFramework.assertEquals(2, h.oldStart);
        TestFramework.assertEquals(5, h.oldCount);
        TestFramework.assertEquals(2, h.newStart);
        TestFramework.assertEquals(5, h.newCount);
        // changed line "3"->"X" sits in the middle: old window 2..6, new window 2..7
        // but merged window new-hi is 7? verify below via line kinds instead
        int deleted = 0;
        int added = 0;
        int context = 0;
        for (HunkLine hl : h.lines) {
            switch (hl.kind) {
                case DELETED -> deleted++;
                case ADDED -> added++;
                case CONTEXT -> context++;
            }
        }
        TestFramework.assertEquals(1, deleted);
        TestFramework.assertEquals(1, added);
        TestFramework.assertEquals(4, context);
    }

    private static void noNewlineMarker() {
        String a = "a\nb";
        String b = "a\nc";
        DiffResult r = d(a, b);
        String ud = UnifiedDiff.render(r);
        // The changed unterminated last lines must carry the marker twice.
        int markers = 0;
        int idx = 0;
        while ((idx = ud.indexOf("\\ No newline at end of file", idx)) >= 0) {
            markers++;
            idx += 3;
        }
        TestFramework.assertTrue(markers >= 2, "expected marker for removed and added bare line, got " + markers);
    }

    private static void minimalWithRepeats() {
        // A case where greedy alignment fails but Myers finds optimum.
        String a = "x\na\nb\nc\ny\n";
        String b = "a\nb\nc\n";
        DiffResult r = d(a, b);
        TestFramework.assertEquals(2, r.editDistance, "only x and y should be deleted");
        assertRoundTrip(a, b);
    }

    private static void coalesced() {
        String a = "a\nb\nc\n";
        String b = "x\ny\nz\n";
        DiffResult r = d(a, b);
        int deletes = 0;
        int inserts = 0;
        for (Edit e : r.edits) {
            if (e.kind == Edit.Kind.DELETE) {
                deletes++;
                TestFramework.assertEquals(3, e.oldCount(), "delete block should be coalesced");
            }
            if (e.kind == Edit.Kind.INSERT) {
                inserts++;
                TestFramework.assertEquals(3, e.newCount(), "insert block should be coalesced");
            }
        }
        TestFramework.assertEquals(1, deletes);
        TestFramework.assertEquals(1, inserts);
        assertRoundTrip(a, b);
    }

    private static void budgetSufficient() {
        String a = "a\nb\n";
        String b = "c\nd\n";
        DiffResult r = new MyersDiff(4, 1_000_000, 3).diff(a, b);
        TestFramework.assertFalse(r.degraded);
        TestFramework.assertTrue(r.shortest);
        TestFramework.assertEquals(4, r.editDistance);
    }

    private static void budgetZeroIdentical() {
        String a = "a\nb\n";
        DiffResult r = new MyersDiff(0, 1_000_000, 3).diff(a, a);
        TestFramework.assertFalse(r.degraded, "identical input found at D=0, no degradation");
        TestFramework.assertEquals(0, r.editDistance);
    }
}
