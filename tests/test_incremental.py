import unittest

from minilang import Document, EditError, full_parse, nodes_equal, tree_signature


SOURCE = "\n".join(
    [
        "let a = 1 + 2;",
        "fn f(x) = x * 2;",
        "let b = f(3);",
        "let c = \"done\";",
    ]
)


class IncrementalReuseTests(unittest.TestCase):
    def test_unchanged_nodes_keep_identity(self):
        doc = Document(SOURCE)
        old_decls = list(doc.result.tree.children)

        # Edit strictly inside the first declaration: change '1' to '11'.
        result = doc.apply_edit(8, 1, "11")
        new_decls = result.tree.children

        self.assertEqual(result.stats["reused"], 3)
        self.assertEqual(result.stats["parsed"], 1)
        # declarations after the edited one are the very same objects
        self.assertIs(new_decls[1], old_decls[1])
        self.assertIs(new_decls[2], old_decls[2])
        self.assertIs(new_decls[3], old_decls[3])
        # the edited declaration was reparsed
        self.assertIsNot(new_decls[0], old_decls[0])

    def test_node_ranges_shifted_after_edit(self):
        doc = Document(SOURCE)
        old_third = doc.result.tree.children[2]
        old_start, old_end = old_third.start, old_third.end

        # Insert 3 characters at offset 0: every declaration moves by 3.
        result = doc.apply_edit(0, 0, "   ")
        fresh = full_parse("   " + SOURCE)

        self.assertEqual(result.stats["reused"], 4)
        third = result.tree.children[2]
        self.assertEqual(third.start, old_start + 3)
        self.assertEqual(third.end, old_end + 3)
        # a nested span inside the reused subtree shifted too
        ident = third.children[0].children[0]
        self.assertEqual(ident.kind, "ident")
        fresh_ident = fresh.tree.children[2].children[0].children[0]
        self.assertEqual(ident.start, fresh_ident.start)
        self.assertTrue(nodes_equal(result.tree, fresh.tree))

    def test_deletion_shifts_back(self):
        doc = Document("//xx\n" + SOURCE)
        result = doc.apply_edit(0, 5, "")  # delete the leading comment
        fresh = full_parse(SOURCE)
        self.assertEqual(result.stats["reused"], 4)
        self.assertTrue(nodes_equal(result.tree, fresh.tree))

    def test_edit_touching_boundary(self):
        # Pure insertion exactly between two declarations: left stays, right
        # shifts and is still reusable.
        doc = Document(SOURCE)
        boundary = SOURCE.index("fn f")
        result = doc.apply_edit(boundary, 0, "  ")
        fresh = full_parse(SOURCE[:boundary] + "  " + SOURCE[boundary:])
        self.assertTrue(nodes_equal(result.tree, fresh.tree))
        self.assertEqual(result.stats["parsed"], 0)

    def test_consecutive_edits_chain(self):
        doc = Document(SOURCE)
        edits = [
            (0, 0, "// hi\n"),       # insert comment at top
            (5, 0, " "),             # whitespace tweak
            (20, 1, "9"),            # replace inside first decl
            (10, 2, ""),             # delete nearby
            (0, 6, ""),              # remove the comment
        ]
        for start, old_len, new_text in edits:
            result = doc.apply_edit(start, old_len, new_text)
            fresh = full_parse(doc.text)
            self.assertTrue(
                nodes_equal(result.tree, fresh.tree),
                msg=f"tree mismatch after edit {(start, old_len, new_text)}",
            )
            self.assertEqual(
                [d.signature() for d in result.diagnostics],
                [d.signature() for d in fresh.diagnostics],
            )

    def test_string_edit_does_not_corrupt_neighbours(self):
        text = 'let a = 1;\nlet s = "x";\nlet b = 2;'
        doc = Document(text)
        old_first = doc.result.tree.children[0]
        old_third = doc.result.tree.children[2]

        # Delete the closing quote of the string: rest of its line becomes
        # string content, recovery must still match a full parse.
        q = text.index('"x"') + 2  # position of closing quote
        result = doc.apply_edit(q, 1, "")
        fresh = full_parse(doc.text)
        self.assertTrue(nodes_equal(result.tree, fresh.tree))
        # declaration before the damage is untouched and reused
        self.assertIs(result.tree.children[0], old_first)
        # third declaration spans must equal the full parse
        self.assertEqual(
            result.tree.children[2].start, fresh.tree.children[2].start
        )

    def test_inserting_quote_changes_lexing_of_following_symbols(self):
        text = "let a = 1; // \"x\nlet b = 2;"
        doc = Document(text)
        # remove the quote that protects the comment... actually build a
        # case where quote insertion swallows following punctuation
        text2 = 'let a = 1; let b = (2);'
        doc = Document(text2)
        pos = text2.index("(2)")  # insert a quote before (
        result = doc.apply_edit(pos, 0, '"')
        fresh = full_parse(doc.text)
        self.assertTrue(nodes_equal(result.tree, fresh.tree))
        self.assertEqual(
            [d.signature() for d in result.diagnostics],
            [d.signature() for d in fresh.diagnostics],
        )

    def test_invalid_edit_raises(self):
        doc = Document("let a = 1;")
        with self.assertRaises(EditError):
            doc.apply_edit(-1, 0, "x")
        with self.assertRaises(EditError):
            doc.apply_edit(5, 99, "x")
        with self.assertRaises(EditError):
            doc.apply_edit(0, 1, None)


class IncrementalEquivalenceTests(unittest.TestCase):
    def _check(self, text, edits):
        doc = Document(text)
        for start, old_len, new_text in edits:
            result = doc.apply_edit(start, old_len, new_text)
            fresh = full_parse(doc.text)
            self.assertEqual(
                tree_signature(result.tree),
                tree_signature(fresh.tree),
                msg=f"tree differs after edit "
                    f"{(start, old_len, new_text)!r} in {doc.text!r}",
            )
            self.assertEqual(
                [d.signature() for d in result.diagnostics],
                [d.signature() for d in fresh.diagnostics],
                msg=f"diagnostics differ after edit "
                    f"{(start, old_len, new_text)!r} in {doc.text!r}",
            )

    def test_unclosed_bracket_then_fixes(self):
        # open a paren, close it, delete again: continuous editing on an
        # initially broken document
        text = "let a = (1 + 2;\nlet b = 3;"
        edits = [
            (text.index(";"), 0, ")"),      # close the paren before ';'
            (14, 1, ""),                    # delete ')' again -> broken
            (8, 1, ""),                     # delete '(' too -> fine
            (8, 0, "("),                    # reinsert '('
        ]
        self._check(text, edits)

    def test_symbols_inside_string_edits(self):
        text = 'let s = "a + b // (x;)";\nlet t = 2;'
        edits = [
            (text.index("//"), 0, "()"),
            (text.index('";'), 1, ""),      # break closing quote
            (text.index("a + b"), 1, ""),
            (0, 0, '"'),                    # stray quote at document start
            (0, 1, ""),                     # remove it
        ]
        self._check(text, edits)

    def test_keyword_typing_character_by_character(self):
        doc_text = "let a = 1;\n\nlet c = 3;"
        start = doc_text.index("\n\n") + 2
        # simulate typing "let b = 2;" one chunk at a time, advancing a cursor
        edits = []
        cursor = start
        for chunk in ("l", "le", "let", "let ", "let b", "let b = 2;"):
            edits.append((cursor, 0, chunk))
            cursor += len(chunk)
        self._check(doc_text, edits)


if __name__ == "__main__":
    unittest.main()
