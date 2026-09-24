package com.example.diff;

import java.util.List;

/** Unit tests for {@link Lines} tokenization: newline form is content. */
public final class LinesTests {

    private LinesTests() {
    }

    public static void register(TestFramework tf) {
        tf.test("empty string -> no lines", LinesTests::empty);
        tf.test("single line with newline -> one terminated line", LinesTests::singleTerminated);
        tf.test("single line without trailing newline -> one unterminated line", LinesTests::singleBare);
        tf.test("LF splits and keeps terminator", LinesTests::lfKept);
        tf.test("CRLF splits and keeps CRLF intact", LinesTests::crlfKept);
        tf.test("mixed LF and CRLF in one text stay distinct lines", LinesTests::mixedEndings);
        tf.test("lone CR is content, not a terminator", LinesTests::loneCr);
        tf.test("offsets point at line starts", LinesTests::offsets);
        tf.test("empty lines (\\n\\n) preserved", LinesTests::emptyLines);
        tf.test("file ending with newline differs from one without", LinesTests::trailingNewlineMatters);
    }

    private static void empty() {
        TestFramework.assertEquals(0, Lines.split("").size());
    }

    private static void singleTerminated() {
        List<Lines.Line> ls = Lines.split("abc\n");
        TestFramework.assertEquals(1, ls.size());
        TestFramework.assertEquals("abc\n", ls.get(0).text);
        TestFramework.assertEquals("abc", ls.get(0).content());
        TestFramework.assertEquals("\n", ls.get(0).ending());
    }

    private static void singleBare() {
        List<Lines.Line> ls = Lines.split("abc");
        TestFramework.assertEquals(1, ls.size());
        TestFramework.assertEquals("abc", ls.get(0).text);
        TestFramework.assertEquals("", ls.get(0).ending());
    }

    private static void lfKept() {
        List<Lines.Line> ls = Lines.split("a\nb\n");
        TestFramework.assertEquals(List.of("a\n", "b\n"), Lines.texts(ls));
    }

    private static void crlfKept() {
        List<Lines.Line> ls = Lines.split("a\r\nb\r\n");
        TestFramework.assertEquals(2, ls.size());
        TestFramework.assertEquals("a\r\n", ls.get(0).text);
        TestFramework.assertEquals("b\r\n", ls.get(1).text);
        TestFramework.assertEquals("\r\n", ls.get(0).ending());
    }

    private static void mixedEndings() {
        List<Lines.Line> ls = Lines.split("a\r\nb\nc");
        TestFramework.assertEquals(List.of("a\r\n", "b\n", "c"), Lines.texts(ls));
        // "a\r\n" and "a\n" must compare different.
        TestFramework.assertFalse(ls.get(0).text.equals("a\n"), "CRLF line must not equal LF line");
    }

    private static void loneCr() {
        List<Lines.Line> ls = Lines.split("a\rb");
        TestFramework.assertEquals(1, ls.size());
        TestFramework.assertEquals("a\rb", ls.get(0).text);
    }

    private static void offsets() {
        List<Lines.Line> ls = Lines.split("ab\ncd\ne");
        TestFramework.assertEquals(0, ls.get(0).offset);
        TestFramework.assertEquals(3, ls.get(1).offset);
        TestFramework.assertEquals(6, ls.get(2).offset);
    }

    private static void emptyLines() {
        List<Lines.Line> ls = Lines.split("\n\nx\n");
        TestFramework.assertEquals(List.of("\n", "\n", "x\n"), Lines.texts(ls));
    }

    private static void trailingNewlineMatters() {
        List<Lines.Line> withNl = Lines.split("a\n");
        List<Lines.Line> noNl = Lines.split("a");
        TestFramework.assertFalse(withNl.get(0).text.equals(noNl.get(0).text),
                "a\\n and a must be different line tokens");
    }
}
