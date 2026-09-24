package com.example.diff;

import java.util.ArrayList;
import java.util.List;

/** Tests for script validation in {@link ApplyEdits}: malformed scripts fail. */
public final class ApplyEditsTests {

    private ApplyEditsTests() {
    }

    public static void register(TestFramework tf) {
        tf.test("valid script applies", ApplyEditsTests::valid);
        tf.test("gap in old coverage is rejected", ApplyEditsTests::gap);
        tf.test("line material mismatch is rejected", ApplyEditsTests::wrongMaterial);
        tf.test("insert with nonzero old range rejected", ApplyEditsTests::badInsertRange);
        tf.test("script not covering all old lines rejected", ApplyEditsTests::incomplete);
        tf.test("equal with mismatched old/new counts rejected", ApplyEditsTests::badEqual);
    }

    private static void valid() {
        MyersDiff d = new MyersDiff();
        DiffResult r = d.diff("a\nb\nc\n", "a\nB\nc\n");
        ApplyEdits.Result ar = ApplyEdits.apply("a\nb\nc\n", r.edits);
        TestFramework.assertTrue(ar.ok, ar.error);
        TestFramework.assertEquals("a\nB\nc\n", ar.text);
    }

    private static void gap() {
        List<Edit> edits = new ArrayList<>();
        // Skip old line index 1 ("b").
        edits.add(new Edit(Edit.Kind.EQUAL, 0, 1, 0, 1, 0, 2, 0, 2,
                List.of("a\n"), List.of("a\n")));
        edits.add(new Edit(Edit.Kind.EQUAL, 2, 3, 1, 2, 4, 6, 4, 6,
                List.of("c\n"), List.of("c\n")));
        ApplyEdits.Result ar = ApplyEdits.apply("a\nb\nc\n", edits);
        TestFramework.assertFalse(ar.ok, "gapped script must fail");
        TestFramework.assertContains(ar.error, "expected");
    }

    private static void wrongMaterial() {
        List<Edit> edits = new ArrayList<>();
        edits.add(new Edit(Edit.Kind.EQUAL, 0, 1, 0, 1, 0, 2, 0, 2,
                List.of("ZZ\n"), List.of("ZZ\n")));
        ApplyEdits.Result ar = ApplyEdits.apply("a\n", edits);
        TestFramework.assertFalse(ar.ok, "wrong context line must fail");
        TestFramework.assertContains(ar.error, "mismatch");
    }

    private static void badInsertRange() {
        List<Edit> edits = new ArrayList<>();
        edits.add(new Edit(Edit.Kind.INSERT, 0, 1, 0, 1, 0, 0, 0, 2,
                new ArrayList<>(), List.of("x\n")));
        ApplyEdits.Result ar = ApplyEdits.apply("a\n", edits);
        TestFramework.assertFalse(ar.ok);
        TestFramework.assertContains(ar.error, "zero old range");
    }

    private static void incomplete() {
        List<Edit> edits = new ArrayList<>();
        edits.add(new Edit(Edit.Kind.EQUAL, 0, 1, 0, 1, 0, 2, 0, 2,
                List.of("a\n"), List.of("a\n")));
        ApplyEdits.Result ar = ApplyEdits.apply("a\nb\n", edits);
        TestFramework.assertFalse(ar.ok);
        TestFramework.assertContains(ar.error, "covers 1 old lines");
    }

    private static void badEqual() {
        List<Edit> edits = new ArrayList<>();
        edits.add(new Edit(Edit.Kind.EQUAL, 0, 1, 0, 2, 0, 2, 0, 4,
                List.of("a\n"), List.of("a\n", "b\n")));
        ApplyEdits.Result ar = ApplyEdits.apply("a\n", edits);
        TestFramework.assertFalse(ar.ok);
        TestFramework.assertContains(ar.error, "different old/new counts");
    }
}
