//! Unit tests for the lexical path/link safety analysis.

use oci_unpack::limits::Limits;
use oci_unpack::path::{normalize_entry_name, validate_symlink_target};

fn lim() -> Limits {
    Limits::default()
}

#[test]
fn accepts_plain_relative_paths() {
    for ok in ["a", "a/b", "a/b/c", "./a", "a//b", "x/.hidden", "a-b/c.d"] {
        let p = normalize_entry_name(ok.as_bytes(), &lim())
            .unwrap_or_else(|e| panic!("{ok} should be accepted: {e}"));
        assert_eq!(p.as_str(), ok.trim_start_matches("./").replace("//", "/"));
    }
}

#[test]
fn rejects_traversal_components() {
    for bad in ["../a", "a/../../b", "..", "a/../..", "x/./../../y"] {
        let e = normalize_entry_name(bad.as_bytes(), &lim()).expect_err(bad);
        assert_eq!(e.code(), "path_traversal", "{bad}: {e}");
    }
}

#[test]
fn rejects_absolute_and_windows() {
    assert_eq!(
        normalize_entry_name("/abs".as_bytes(), &lim())
            .unwrap_err()
            .code(),
        "path_traversal"
    );
    assert_eq!(
        normalize_entry_name("c:\\win".as_bytes(), &lim())
            .unwrap_err()
            .code(),
        "path_traversal"
    );
}

#[test]
fn symlink_contained_targets_ok() {
    let link = normalize_entry_name("a/b/link".as_bytes(), &lim()).unwrap();
    // Stays inside a/b.
    validate_symlink_target(&link, b"sibling", &lim()).unwrap();
    validate_symlink_target(&link, b"../c", &lim()).unwrap(); // → a/c
    validate_symlink_target(&link, b"../../x/y", &lim()).unwrap(); // → x/y
}

#[test]
fn symlink_escaping_targets_rejected() {
    let link = normalize_entry_name("a/b/link".as_bytes(), &lim()).unwrap();
    for bad in ["../../../escape", "/etc", "../../../../x", "/"] {
        assert!(
            validate_symlink_target(&link, bad.as_bytes(), &lim()).is_err(),
            "{bad} should escape"
        );
    }

    // A top-level link may not use ".." at depth zero.
    let top = normalize_entry_name("link".as_bytes(), &lim()).unwrap();
    assert!(validate_symlink_target(&top, b"../escape", &lim()).is_err());
    assert!(validate_symlink_target(&top, b"/abs", &lim()).is_err());
}
