//! Layer semantics: delete-then-recreate, opaque directories, multi-layer
//! overrides, hard links, symlinks.

mod common;

use common::Env;
use fixture_maker::{evil, Compression, LayerSpec, RawEntry as E};
use oci_unpack::Error;

#[test]
fn delete_then_recreate() {
    let env = Env::new();
    env.make(
        "img",
        &[
            LayerSpec::gzip(vec![
                E::file("a.txt", b"v0\n".to_vec()),
                E::file("gone.txt", b"old\n".to_vec()),
            ]),
            LayerSpec::gzip(vec![
                E::whiteout("", "gone.txt"),
                E::file("gone.txt", b"reborn\n".to_vec()),
            ]),
        ],
    );

    let result = env.rebuild("img").expect("rebuild");
    assert_eq!(env.read("img", "a.txt"), "v0\n");
    assert_eq!(env.read("img", "gone.txt"), "reborn\n");

    // gone.txt must be attributed to the layer that re-created it (layer 1).
    let gone = result
        .files
        .iter()
        .find(|f| f.path == "gone.txt")
        .expect("provenance entry");
    assert_eq!(gone.layer_index, 1);
    assert_eq!(gone.kind, "file");

    // The final digest is a real 64-hex sha256 and is stable/reproducible.
    assert!(result.final_digest.starts_with("sha256:"));
    let again = env.rebuild("img").expect("rebuild again");
    assert_eq!(
        again.final_digest, result.final_digest,
        "digest reproducible"
    );
}

#[test]
fn same_path_overridden_across_layers() {
    let env = Env::new();
    env.make(
        "img",
        &[
            LayerSpec::gzip(vec![E::file("x", b"one".to_vec())]),
            LayerSpec::gzip(vec![E::file("x", b"two".to_vec())]),
            LayerSpec::gzip(vec![E::file("x", b"three".to_vec())]),
        ],
    );
    let result = env.rebuild("img").expect("rebuild");
    assert_eq!(env.read("img", "x"), "three");
    let x = result.files.iter().find(|f| f.path == "x").unwrap();
    assert_eq!(x.layer_index, 2, "provenance points at last writer");
    assert_eq!(result.layers.len(), 3);
}

#[test]
fn opaque_directory_shadows_parent_content() {
    let env = Env::new();
    env.make(
        "img",
        &[
            LayerSpec::gzip(vec![
                E::dir("etc/app"),
                E::file("etc/app/old", b"old".to_vec()),
                E::file("etc/app/nested/deep", b"deep".to_vec()),
                E::file("etc/sibling", b"keep".to_vec()),
            ]),
            LayerSpec::gzip(vec![
                E::dir("etc/app"),
                E::opaque("etc/app"),
                E::file("etc/app/new", b"new".to_vec()),
            ]),
        ],
    );
    let result = env.rebuild("img").expect("rebuild");

    assert!(!env.rootfs("img", "etc/app/old").exists());
    assert!(!env.rootfs("img", "etc/app/nested/deep").exists());
    assert_eq!(env.read("img", "etc/app/new"), "new");
    assert_eq!(env.read("img", "etc/sibling"), "keep");

    // Nothing below etc/app is attributed to layer 0 anymore.
    for f in &result.files {
        if f.path.starts_with("etc/app/") {
            assert_eq!(f.layer_index, 1, "{} should be layer 1", f.path);
        }
    }
    assert_eq!(result.layers[1].opaque_dirs, 1);
}

#[test]
fn whiteout_removes_directory_subtree() {
    let env = Env::new();
    env.make(
        "img",
        &[
            LayerSpec::gzip(vec![
                E::dir("d/sub"),
                E::file("d/sub/a", b"a".to_vec()),
                E::file("d/b", b"b".to_vec()),
            ]),
            LayerSpec::gzip(vec![E::whiteout("", "d")]),
        ],
    );
    let result = env.rebuild("img").expect("rebuild");
    assert!(!env.rootfs("img", "d").exists(), "whole subtree removed");
    assert!(!result.files.iter().any(|f| f.path.starts_with("d")));
    assert_eq!(result.layers[1].whiteouts, 1);
}

#[test]
fn hardlink_and_contained_symlink_survive() {
    let env = Env::new();
    env.make(
        "img",
        &[LayerSpec::gzip(vec![
            E::file_mode("bin/orig", 0o755, b"#!/bin/sh\n".to_vec()),
            E::hardlink("bin/alias", "bin/orig"),
            E::symlink("bin/link", "orig"),
        ])],
    );
    let _ = env.rebuild("img").expect("rebuild");
    assert_eq!(env.file_kind("img", "bin/alias"), "file");
    assert_eq!(env.file_kind("img", "bin/link"), "symlink");
    assert_eq!(env.read("img", "bin/alias"), "#!/bin/sh\n");
    // Hard link shares the inode of the target.
    use std::os::unix::fs::MetadataExt;
    let o = std::fs::metadata(env.rootfs("img", "bin/orig")).unwrap();
    let a = std::fs::metadata(env.rootfs("img", "bin/alias")).unwrap();
    assert_eq!(
        MetadataExt::ino(&o),
        MetadataExt::ino(&a),
        "hard link shares inode"
    );
}

#[test]
fn directory_replaced_by_file_and_vice_versa() {
    let env = Env::new();
    env.make(
        "img",
        &[
            // layer0: path is a directory with content
            LayerSpec::gzip(vec![E::dir("p"), E::file("p/inner", b"x".to_vec())]),
            // layer1: same path becomes a regular file (subtree must be cleared)
            LayerSpec::gzip(vec![E::file("p", b"now a file".to_vec())]),
        ],
    );
    let _ = env.rebuild("img").expect("rebuild");
    assert_eq!(env.file_kind("img", "p"), "file");
    assert_eq!(env.read("img", "p"), "now a file");
    assert!(!env.rootfs("img", "p/inner").exists());
}

// ───────────────────────── rejections ─────────────────────────

fn assert_rejected<T>(r: oci_unpack::Result<T>, expected_code: &str) {
    match r {
        Err(e) => assert_eq!(e.code(), expected_code, "unexpected error: {e}"),
        Ok(_) => panic!("expected rejection {expected_code}, but rebuild succeeded"),
    }
}

#[test]
fn rejects_path_traversal() {
    let env = Env::new();
    env.make(
        "evil",
        &[LayerSpec::gzip(vec![
            E::file("safe", b"ok".to_vec()),
            evil::traversal_escape(),
        ])],
    );
    assert_rejected(env.rebuild("evil"), "path_traversal");
    assert!(!env.published("evil"), "no partial root published");
}

#[test]
fn rejects_absolute_path() {
    let env = Env::new();
    env.make("evil", &[LayerSpec::gzip(vec![evil::absolute_path()])]);
    assert_rejected(env.rebuild("evil"), "path_traversal");
    assert!(!env.published("evil"));
}

#[test]
fn rejects_escaping_symlink_deep() {
    let env = Env::new();
    env.make(
        "evil",
        &[LayerSpec::gzip(vec![
            evil::symlink_dotdot_deep(),
            E::file("a/b/link/inside", b"x".to_vec()),
        ])],
    );
    assert_rejected(env.rebuild("evil"), "link_escape");
    assert!(!env.published("evil"));
}

#[test]
fn rejects_absolute_symlink() {
    let env = Env::new();
    env.make("evil", &[LayerSpec::gzip(vec![E::symlink("h", "/etc")])]);
    assert_rejected(env.rebuild("evil"), "link_escape");
    assert!(!env.published("evil"));
}

#[test]
fn rejects_device_file() {
    let env = Env::new();
    env.make(
        "evil",
        &[LayerSpec::gzip(vec![
            E::file("ok", b"ok".to_vec()),
            evil::device(),
        ])],
    );
    assert_rejected(env.rebuild("evil"), "unsafe_entry_type");
    assert!(!env.published("evil"));
}

#[test]
fn rejects_corrupt_truncated_gzip() {
    let env = Env::new();
    env.make(
        "evil",
        &[
            LayerSpec::gzip(vec![E::file("base", b"b".to_vec())]),
            LayerSpec {
                entries: vec![E::file("x", b"x".to_vec())],
                compression: Compression::GzipTruncated,
                force_descriptor_digest: None,
                force_diff_id: None,
            },
        ],
    );
    assert_rejected(env.rebuild("evil"), "gzip_error");
    // Layer 0 was fine, but because layer 1 fails nothing may be published.
    assert!(!env.published("evil"));
}

#[test]
fn rejects_non_tar_gzip_garbage() {
    let env = Env::new();
    env.make(
        "evil",
        &[LayerSpec {
            entries: vec![],
            compression: Compression::GarbageGzip,
            force_descriptor_digest: None,
            force_diff_id: None,
        }],
    );
    // Inflates fine but is not a tar → tar_error (honest distinction).
    assert_rejected(env.rebuild("evil"), "tar_error");
    assert!(!env.published("evil"));
}

#[test]
fn rejects_digest_tampering() {
    let env = Env::new();
    env.make(
        "evil",
        &[LayerSpec {
            entries: vec![E::file("ok", b"ok".to_vec())],
            compression: Compression::Gzip,
            force_descriptor_digest: Some(
                "sha256:0000000000000000000000000000000000000000000000000000000000000000".into(),
            ),
            force_diff_id: None,
        }],
    );
    match env.rebuild("evil") {
        Err(Error::DigestMismatch {
            expected, actual, ..
        }) => {
            assert!(expected.starts_with("sha256:0000"));
            assert_ne!(expected, actual);
        }
        other => panic!("expected DigestMismatch, got {other:?}"),
    }
    assert!(!env.published("evil"));
}

#[test]
fn rejects_cross_layer_write_through_symlink_ancestor() {
    // Layer 0 creates a directory-style symlink that escapes (../../x).
    // Layer 1 then creates a regular file *beneath* that link path. Even
    // though layer 1's own file name is a clean relative path, writing through
    // the ancestor symlink would land outside the rootfs → must be rejected.
    let env = Env::new();
    env.make(
        "evil",
        &[
            LayerSpec::gzip(vec![evil::symlink_dotdot_deep()]),
            LayerSpec::gzip(vec![E::file("a/b/link/evil_file", b"x".to_vec())]),
        ],
    );
    // Layer 0 itself already fails link validation, so reject either code;
    // the essential guarantee is no publish. To isolate the ancestor check we
    // also test the contained-but-shielding case below.
    let r = env.rebuild("evil");
    assert!(r.is_err());
    assert!(!env.published("evil"));
}

#[test]
fn rejects_write_beneath_contained_symlink_to_avoid_host_write() {
    // A symlink to a contained dir still means a later layer writing under it
    // follows the link. Our policy forbids creating a new file beneath any
    // ancestor that is a symlink (defence in depth against host writes), while
    // the link itself and its target remain allowed.
    let env = Env::new();
    env.make(
        "img",
        &[
            LayerSpec::gzip(vec![
                E::dir("real/sub"),
                E::file("real/sub/existing", b"ok".to_vec()),
                E::symlink("linkdir", "real"),
            ]),
            LayerSpec::gzip(vec![E::file("linkdir/sub/new", b"n".to_vec())]),
        ],
    );
    let err = env
        .rebuild("img")
        .expect_err("writing under symlink ancestor rejected");
    assert_eq!(err.code(), "ancestor_symlink", "got: {err}");
    assert!(!env.published("img"));
}

#[test]
fn contained_symlink_itself_is_preserved() {
    // The contained symlink and the file it points at are both legitimate;
    // only *new writes beneath the link* are forbidden.
    let env = Env::new();
    env.make(
        "img",
        &[LayerSpec::gzip(vec![
            E::dir("real"),
            E::file("real/data", b"payload".to_vec()),
            E::symlink("linkdir", "real"),
        ])],
    );
    let _ = env.rebuild("img").expect("valid image with symlink");
    assert_eq!(env.file_kind("img", "linkdir"), "symlink");
    assert_eq!(env.read("img", "real/data"), "payload");
}

#[test]
fn rejects_hardlink_to_missing_target() {
    let env = Env::new();
    env.make(
        "evil",
        &[LayerSpec::gzip(vec![E::hardlink(
            "alias",
            "does/not/exist",
        )])],
    );
    let err = env
        .rebuild("evil")
        .expect_err("dangling hard link rejected");
    // Normalize rejects "does/not/exist" only if invalid; here it is valid but
    // absent at resolution time → conflict.
    assert_eq!(err.code(), "conflict", "got: {err}");
    assert!(!env.published("evil"));
}

#[test]
fn rejects_hardlink_to_directory() {
    let env = Env::new();
    env.make(
        "evil",
        &[LayerSpec::gzip(vec![
            E::dir("adir"),
            E::hardlink("alink", "adir"),
        ])],
    );
    let err = env.rebuild("evil").expect_err("hardlink to dir rejected");
    assert_eq!(err.code(), "conflict", "got: {err}");
}

#[test]
fn opaque_dir_then_followed_by_normal_layer_keeps_new_content() {
    // Opaque applies only to layers below it; a still-later layer can add.
    let env = Env::new();
    env.make(
        "img",
        &[
            LayerSpec::gzip(vec![E::dir("etc"), E::file("etc/old", b"o".to_vec())]),
            LayerSpec::gzip(vec![E::opaque("etc")]),
            LayerSpec::gzip(vec![E::file("etc/fresh", b"f".to_vec())]),
        ],
    );
    let _ = env.rebuild("img").expect("rebuild");
    assert!(!env.rootfs("img", "etc/old").exists());
    assert_eq!(env.read("img", "etc/fresh"), "f");
}

#[test]
fn rejects_uncompressed_diff_id_tampering() {
    // The gzip blob and manifest digest are genuinely valid; only the config's
    // uncompressed diff_id lies. This proves the decompressed tar is hashed
    // for real rather than trusting metadata.
    let env = Env::new();
    env.make(
        "evil",
        &[LayerSpec {
            entries: vec![E::file("ok", b"ok\n".to_vec())],
            compression: Compression::Gzip,
            force_descriptor_digest: None,
            force_diff_id: Some(
                "sha256:1111111111111111111111111111111111111111111111111111111111111111".into(),
            ),
        }],
    );
    match env.rebuild("evil") {
        Err(Error::DigestMismatch {
            expected, actual, ..
        }) => {
            assert!(expected.starts_with("sha256:1111"));
            assert_ne!(expected, actual);
            // The reported actual digest must be a real 64-hex value.
            assert_eq!(actual.len(), 7 + 64);
        }
        other => panic!("expected DigestMismatch, got {other:?}"),
    }
    assert!(!env.published("evil"));
}

#[test]
fn later_good_build_is_not_blocked_by_prior_failure() {
    // First attempt is malicious (fails, publishes nothing); then the same name
    // is rebuilt from a valid fixture and must succeed.
    let env = Env::new();
    env.make("img", &[LayerSpec::gzip(vec![evil::absolute_path()])]);
    assert!(env.rebuild("img").is_err());

    // Replace with benign content.
    std::fs::remove_dir_all(env.fixtures.join("img")).unwrap();
    env.make(
        "img",
        &[LayerSpec::gzip(vec![E::file("ok", b"yes".to_vec())])],
    );
    let r = env.rebuild("img").expect("rebuild after fixed fixture");
    assert_eq!(r.status, "published");
    assert_eq!(env.read("img", "ok"), "yes");
}
