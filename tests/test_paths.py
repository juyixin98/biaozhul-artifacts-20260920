"""路径安全测试：词法穿越与符号链接逃逸。"""

import os
import tempfile
import unittest
from pathlib import Path

from sig_manifest.errors import UnsafePathError
from sig_manifest.paths import resolve_within, validate_relative, walk_artifact_files


class TestLexicalValidation(unittest.TestCase):
    def test_simple_paths_ok(self) -> None:
        for p in ("a", "a/b", "a/b/c.sh", ".hidden", "x/y/z.txt"):
            with self.subTest(p=p):
                self.assertEqual(validate_relative(p), p)

    def test_dotdot_rejected(self) -> None:
        for p in ("../etc/passwd", "a/../../b", "..", "x/../..", "foo/../bar"):
            with self.subTest(p=p):
                with self.assertRaises(UnsafePathError):
                    validate_relative(p)

    def test_absolute_rejected(self) -> None:
        for p in ("/etc/passwd", "/", "//server/share"):
            with self.subTest(p=p):
                with self.assertRaises(UnsafePathError):
                    validate_relative(p)

    def test_backslash_rejected(self) -> None:
        for p in ("a\\b", "..\\..\\windows", "a/b\\c"):
            with self.subTest(p=p):
                with self.assertRaises(UnsafePathError):
                    validate_relative(p)

    def test_drive_and_special_rejected(self) -> None:
        for p in ("C:/windows", "C:foo", "a//b", "a/./b", "", "a/b/"):
            with self.subTest(p=p):
                with self.assertRaises(UnsafePathError):
                    validate_relative(p)

    def test_nul_and_control_rejected(self) -> None:
        with self.assertRaises(UnsafePathError):
            validate_relative("a\x00b")
        with self.assertRaises(UnsafePathError):
            validate_relative("a\x01b")


class TestResolveWithin(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "artifact"
        self.root.mkdir()
        (self.root / "sub").mkdir()
        (self.root / "sub" / "file.txt").write_text("ok", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_normal_file_resolves(self) -> None:
        resolved = resolve_within(self.root, "sub/file.txt")
        self.assertEqual(resolved, (self.root / "sub" / "file.txt").resolve())

    def test_nonexistent_target_stays_inside(self) -> None:
        resolved = resolve_within(self.root, "sub/new/file.bin")
        self.assertTrue(str(resolved).startswith(str(self.root.resolve())))

    def test_missing_root_rejected(self) -> None:
        with self.assertRaises(UnsafePathError):
            resolve_within(self.root / "does-not-exist", "a")

    def test_symlink_to_file_outside_rejected(self) -> None:
        outside = self.root.parent / "secret.txt"
        outside.write_text("top secret", encoding="utf-8")
        link = self.root / "evil-link"
        os.symlink(outside, link)
        with self.assertRaises(UnsafePathError):
            resolve_within(self.root, "evil-link")

    def test_symlink_dir_outside_rejected(self) -> None:
        outside_dir = self.root.parent / "secret-dir"
        outside_dir.mkdir(exist_ok=True)
        (outside_dir / "data").write_text("x", encoding="utf-8")
        link = self.root / "evil-dir"
        os.symlink(outside_dir, link)
        with self.assertRaises(UnsafePathError):
            resolve_within(self.root, "evil-dir/data")

    def test_symlink_inside_root_allowed(self) -> None:
        target = self.root / "sub" / "file.txt"
        link = self.root / "alias.txt"
        os.symlink(target, link)
        resolved = resolve_within(self.root, "alias.txt")
        self.assertTrue(resolved.is_file())

    def test_walk_detects_escaping_link(self) -> None:
        outside = self.root.parent / "secret2.txt"
        outside.write_text("secret", encoding="utf-8")
        os.symlink(outside, self.root / "link2")
        with self.assertRaises(UnsafePathError):
            walk_artifact_files(self.root)

    def test_walk_dangling_link_is_error(self) -> None:
        os.symlink(self.root / "missing-target", self.root / "dangling")
        with self.assertRaises(UnsafePathError):
            walk_artifact_files(self.root)

    def test_walk_lists_all_sorted(self) -> None:
        files = walk_artifact_files(self.root)
        self.assertEqual(files, ["sub/file.txt"])


if __name__ == "__main__":
    unittest.main()
