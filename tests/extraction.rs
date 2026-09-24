//! End-to-end extraction behaviour on real OCI fixtures.
mod common;

use std::collections::HashMap;

use common::{
    build_image, exists_in_rootfs, read_rootfs, tmp_workdir, workdir_for, Entry, Layer, RawEntry,
};
use oci_unpack::{AppError, Limits};

fn tight_limits() -> Limits {
    // Small, explicit limits for bomb/overflow tests.
    Limits {
        max_layer_uncompressed: 4096,
        max_entries_per_layer: 100,
        max_total_uncompressed: 8192,
        max_path_len: 256,
        max_file_size: 4096,
        max_file_count: 200,
        max_verify_bytes: 64 * 1024,
    }
}

fn err_chain(e: &AppError) -> String {
    e.to_string()
}

// --------------------------------------------------------------------------
// 1. Multi-layer overlay + provenance + digest
// --------------------------------------------------------------------------

#[test]
fn multi_layer_overlay_and_provenance() {
    let tmp = tmp_workdir();
    let w = workdir_for(tmp.path());

    let img = build_image(
        &w.images_dir(),
        "overlay",
        vec![
            Layer::new(vec![
                Entry::Dir("etc"),
                Entry::File("etc/hosts", b"base hosts\n".to_vec(), 0o644),
                Entry::File("app/v1", b"version-one\n".to_vec(), 0o644),
                Entry::Symlink {
                    link: "app/current",
                    target: "v1",
                },
            ]),
            Layer::new(vec![
                // overwrite a file (same path, multiple layers)
                Entry::File("etc/hosts", b"new hosts\n".to_vec(), 0o644),
                // overwrite with a different mode too
                Entry::File("app/v1", b"version-two\n".to_vec(), 0o600),
            ]),
        ],
        None,
    );

    let report = w.rebuild("overlay", &Limits::default()).expect("rebuild");
    assert_eq!(report.layer_count, 2);
    assert!(report.rootfs_digest.starts_with("sha256:"));
    assert_eq!(
        read_rootfs(&w, "overlay", "etc/hosts").as_deref(),
        Some(&b"new hosts\n"[..])
    );
    assert_eq!(
        read_rootfs(&w, "overlay", "app/v1").as_deref(),
        Some(&b"version-two\n"[..])
    );

    // Provenance points to the correct (last) layer.
    let hosts = report.files.iter().find(|f| f.path == "etc/hosts").unwrap();
    assert_eq!(hosts.layer_index, 1);
    assert_eq!(hosts.source_layer, img.layer_digests[1]);
    assert_eq!(hosts.kind, "file");
    assert_eq!(
        hosts.content_sha256.as_deref(),
        Some(img.file_hashes["etc/hosts"].as_str())
    );
    assert_eq!(hosts.mode, "0644");

    let v1 = report.files.iter().find(|f| f.path == "app/v1").unwrap();
    assert_eq!(v1.layer_index, 1);
    assert_eq!(v1.mode, "0600");

    // Symlink survived from layer 0 with layer-0 provenance.
    let link = report
        .files
        .iter()
        .find(|f| f.path == "app/current")
        .unwrap();
    assert_eq!(link.kind, "symlink");
    assert_eq!(link.symlink_target.as_deref(), Some("v1"));
    assert_eq!(link.layer_index, 0);

    // Determinism: same image rebuilds to the same rootfs digest.
    let again = w.rebuild("overlay", &Limits::default()).unwrap();
    assert_eq!(again.rootfs_digest, report.rootfs_digest);
}

// --------------------------------------------------------------------------
// 2. Delete then recreate (same layer and across layers)
// --------------------------------------------------------------------------

#[test]
fn whiteout_then_recreate_same_layer() {
    let tmp = tmp_workdir();
    let w = workdir_for(tmp.path());
    build_image(
        &w.images_dir(),
        "drc",
        vec![
            Layer::new(vec![
                Entry::Dir("d"),
                Entry::File("d/a", b"old".to_vec(), 0o644),
                Entry::File("keep", b"keep".to_vec(), 0o644),
            ]),
            // Same layer: delete d/a, then recreate it with new content.
            Layer::new(vec![
                Entry::File("d/.wh.a", vec![], 0o000),
                Entry::File("d/a", b"new".to_vec(), 0o644),
                // delete "keep" entirely
                Entry::File(".wh.keep", vec![], 0o000),
            ]),
        ],
        None,
    );

    let report = w.rebuild("drc", &Limits::default()).unwrap();
    assert_eq!(read_rootfs(&w, "drc", "d/a").as_deref(), Some(&b"new"[..]));
    assert!(!exists_in_rootfs(&w, "drc", "keep"));

    // The whiteout marker itself must not be materialised.
    assert!(!exists_in_rootfs(&w, "drc", "d/.wh.a"));
    assert!(!exists_in_rootfs(&w, "drc", ".wh.keep"));

    // Recreated file is attributed to layer 1; no provenance for deleted.
    let a = report.files.iter().find(|f| f.path == "d/a").unwrap();
    assert_eq!(a.layer_index, 1);
    assert!(report.files.iter().all(|f| f.path != "keep"));
}

// --------------------------------------------------------------------------
// 3. Parent directory masking (opaque whiteout)
// --------------------------------------------------------------------------

#[test]
fn opaque_directory_masks_parent_contents() {
    let tmp = tmp_workdir();
    let w = workdir_for(tmp.path());
    build_image(
        &w.images_dir(),
        "opaque",
        vec![
            Layer::new(vec![
                Entry::Dir("etc"),
                Entry::Dir("etc/nested"),
                Entry::File("etc/old1", b"1".to_vec(), 0o644),
                Entry::File("etc/nested/old2", b"2".to_vec(), 0o644),
            ]),
            // Opaque etc/ + only one fresh child.
            Layer::new(vec![
                Entry::Dir("etc"),
                Entry::File("etc/.wh..wh..opq", vec![], 0o000),
                Entry::File("etc/new1", b"3".to_vec(), 0o644),
            ]),
        ],
        None,
    );

    let report = w.rebuild("opaque", &Limits::default()).unwrap();
    assert!(!exists_in_rootfs(&w, "opaque", "etc/old1"));
    assert!(!exists_in_rootfs(&w, "opaque", "etc/nested/old2"));
    assert!(!exists_in_rootfs(&w, "opaque", "etc/nested"));
    assert!(!exists_in_rootfs(&w, "opaque", "etc/.wh..wh..opq"));
    assert_eq!(
        read_rootfs(&w, "opaque", "etc/new1").as_deref(),
        Some(&b"3"[..])
    );

    // Directory itself remains and is attributed to the opaque layer.
    let etc = report.files.iter().find(|f| f.path == "etc").unwrap();
    assert_eq!(etc.layer_index, 1);
}

// --------------------------------------------------------------------------
// 3b. Opaque marker placed where a previous layer had a symlink: the link
//     must be removed (never followed) and a real directory materialised.
// --------------------------------------------------------------------------

#[test]
fn opaque_over_previous_symlink_becomes_real_directory() {
    let tmp = tmp_workdir();
    let w = workdir_for(tmp.path());
    build_image(
        &w.images_dir(),
        "opaque-link",
        vec![
            Layer::new(vec![
                Entry::Dir("outside"),
                Entry::File("outside/secret", b"secret\n".to_vec(), 0o644),
                // link pointing out of the path where the next layer
                // declares an opaque directory
                Entry::Symlink {
                    link: "mnt",
                    target: "outside",
                },
            ]),
            Layer::new(vec![
                Entry::Dir("mnt"),
                Entry::File("mnt/.wh..wh..opq", vec![], 0o000),
                Entry::File("mnt/owned", b"owned\n".to_vec(), 0o644),
            ]),
        ],
        None,
    );
    let report = w.rebuild("opaque-link", &Limits::default()).unwrap();

    // mnt is now a real in-root directory, not a symlink.
    let mnt = report.files.iter().find(|f| f.path == "mnt").unwrap();
    assert_eq!(mnt.kind, "directory");
    assert_eq!(mnt.layer_index, 1);
    let root = w.published_root("opaque-link").unwrap().unwrap();
    assert!(root.join("mnt").symlink_metadata().unwrap().is_dir());
    assert_eq!(std::fs::read(root.join("mnt/owned")).unwrap(), b"owned\n");
    // The target the old symlink used to point at is untouched.
    assert_eq!(
        std::fs::read(root.join("outside/secret")).unwrap(),
        b"secret\n"
    );
    // Writing under mnt/ landed in-root, never at outside/owned.
    assert!(!root.join("outside/owned").exists());
}

// --------------------------------------------------------------------------
// 4a. Path traversal entries are rejected
// --------------------------------------------------------------------------

#[test]
fn rejects_path_traversal() {
    let tmp = tmp_workdir();
    let w = workdir_for(tmp.path());
    build_image(
        &w.images_dir(),
        "trav",
        vec![Layer::new(vec![
            Entry::File("safe", b"x".to_vec(), 0o644),
            RawEntry::traversal_file("../evil", b"x"),
        ])],
        None,
    );
    let e = w.rebuild("trav", &Limits::default()).unwrap_err();
    assert!(matches!(e, AppError::RejectedEntry { .. }), "got {e}");
    assert!(err_chain(&e).contains("`..`"));
    // No partial root published.
    assert!(w.published_root("trav").unwrap().is_none());
}

#[test]
fn rejects_absolute_path() {
    let tmp = tmp_workdir();
    let w = workdir_for(tmp.path());
    build_image(
        &w.images_dir(),
        "abs",
        vec![Layer::new(vec![RawEntry::traversal_file(
            "/tmp/abs-evil",
            b"x",
        )])],
        None,
    );
    let e = w.rebuild("abs", &Limits::default()).unwrap_err();
    assert!(matches!(e, AppError::RejectedEntry { .. }), "got {e}");
}

// --------------------------------------------------------------------------
// 4b. Escaping symlinks are rejected (multiple shapes)
// --------------------------------------------------------------------------

#[test]
fn rejects_escaping_symlink_parent_ref() {
    let tmp = tmp_workdir();
    let w = workdir_for(tmp.path());
    build_image(
        &w.images_dir(),
        "ln1",
        vec![Layer::new(vec![
            Entry::Dir("d"),
            Entry::Symlink {
                link: "d/escape",
                target: "../../../etc/passwd",
            },
        ])],
        None,
    );
    let e = w.rebuild("ln1", &Limits::default()).unwrap_err();
    assert!(matches!(e, AppError::RejectedEntry { .. }), "got {e}");
}

#[test]
fn rejects_absolute_symlink() {
    let tmp = tmp_workdir();
    let w = workdir_for(tmp.path());
    build_image(
        &w.images_dir(),
        "ln2",
        vec![Layer::new(vec![Entry::Symlink {
            link: "evil",
            target: "/etc/passwd",
        }])],
        None,
    );
    let e = w.rebuild("ln2", &Limits::default()).unwrap_err();
    assert!(matches!(e, AppError::RejectedEntry { .. }), "got {e}");
}

#[test]
fn chained_confined_symlinks_resolve_and_escape_chain_is_rejected() {
    // a -> . (confined), via-a -> a/../../x  (lexically escapes -> reject)
    let tmp = tmp_workdir();
    let w = workdir_for(tmp.path());
    build_image(
        &w.images_dir(),
        "ln3",
        vec![Layer::new(vec![
            Entry::Symlink {
                link: "a",
                target: ".",
            },
            Entry::Symlink {
                link: "via",
                target: "a/../../etc",
            },
        ])],
        None,
    );
    let e = w.rebuild("ln3", &Limits::default()).unwrap_err();
    assert!(matches!(e, AppError::RejectedEntry { .. }), "got {e}");
}

// --------------------------------------------------------------------------
// 4c. Device files / fifos are rejected
// --------------------------------------------------------------------------

#[test]
fn rejects_character_device() {
    let tmp = tmp_workdir();
    let w = workdir_for(tmp.path());
    build_image(
        &w.images_dir(),
        "dev",
        vec![Layer::new(vec![Entry::Char {
            link: "dev/null",
            major: 1,
            minor: 3,
        }])],
        None,
    );
    let e = w.rebuild("dev", &Limits::default()).unwrap_err();
    assert!(matches!(e, AppError::RejectedEntry { .. }), "got {e}");
    assert!(err_chain(&e).contains("device"));
}

#[test]
fn rejects_fifo() {
    let tmp = tmp_workdir();
    let w = workdir_for(tmp.path());
    build_image(
        &w.images_dir(),
        "fifo",
        vec![Layer::new(vec![Entry::Fifo("run/pipe")])],
        None,
    );
    let e = w.rebuild("fifo", &Limits::default()).unwrap_err();
    assert!(matches!(e, AppError::RejectedEntry { .. }), "got {e}");
}

// --------------------------------------------------------------------------
// 5. Corrupt gzip layer
// --------------------------------------------------------------------------

#[test]
fn rejects_corrupt_gzip_layer() {
    let tmp = tmp_workdir();
    let w = workdir_for(tmp.path());
    build_image(
        &w.images_dir(),
        "badgz",
        vec![
            Layer::new(vec![Entry::File("a", b"a".to_vec(), 0o644)]),
            Layer::new(vec![Entry::File("b", b"b".to_vec(), 0o644)]).corrupt(),
        ],
        None,
    );
    let e = w.rebuild("badgz", &Limits::default()).unwrap_err();
    assert!(
        matches!(e, AppError::CorruptArchive(_)) || matches!(e, AppError::DigestMismatch { .. }),
        "got {e:?}"
    );
    assert!(w.published_root("badgz").unwrap().is_none());
}

// --------------------------------------------------------------------------
// 6. Tampered blob (digest mismatch) — nothing applied
// --------------------------------------------------------------------------

#[test]
fn rejects_digest_mismatch_and_publishes_nothing() {
    let tmp = tmp_workdir();
    let w = workdir_for(tmp.path());
    let mut tamper = HashMap::new();
    // The manifest still references the digest of the original bytes; the
    // stored blob is overwritten with different content.
    tamper.insert(0usize, b"totally different blob bytes".to_vec());
    build_image(
        &w.images_dir(),
        "tamper",
        vec![Layer::new(vec![Entry::File("a", b"a".to_vec(), 0o644)])],
        Some(tamper),
    );
    let e = w.rebuild("tamper", &Limits::default()).unwrap_err();
    assert!(matches!(e, AppError::DigestMismatch { .. }), "got {e:?}");
    assert!(w.published_root("tamper").unwrap().is_none());
}

// --------------------------------------------------------------------------
// 7. Over-extraction limits (decompression bomb)
// --------------------------------------------------------------------------

#[test]
fn rejects_oversized_layer() {
    let tmp = tmp_workdir();
    let w = workdir_for(tmp.path());
    let big = vec![b'z'; 5000]; // > 4096 per-layer limit
    build_image(
        &w.images_dir(),
        "bomb",
        vec![Layer::new(vec![Entry::File("big", big, 0o644)]).plain()],
        None,
    );
    let e = w.rebuild("bomb", &tight_limits()).unwrap_err();
    assert!(matches!(e, AppError::Limit(_)), "got {e:?}");
    assert!(w.published_root("bomb").unwrap().is_none());
}

#[test]
fn rejects_gzip_bomb_during_verification() {
    // 20 KiB of zeros gzips to a few dozen stored bytes; decompressed size
    // is far above the 4096-byte limit and must trip verification.
    let tmp = tmp_workdir();
    let w = workdir_for(tmp.path());
    let bomb = vec![0u8; 20 * 1024];
    build_image(
        &w.images_dir(),
        "gz-bomb",
        vec![Layer::new(vec![Entry::File("zeros", bomb, 0o644)])],
        None,
    );
    let e = w.rebuild("gz-bomb", &tight_limits()).unwrap_err();
    assert!(matches!(e, AppError::Limit(_)), "got {e:?}");
    assert!(w.published_root("gz-bomb").unwrap().is_none());
}

#[test]
fn later_layer_never_writes_through_an_existing_symlink() {
    // Layer 0: confined symlink s -> inner
    // Layer 1: regular file s/leaf — the extractor must not traverse the
    // link; it replaces the link with a real directory inside the root.
    let tmp = tmp_workdir();
    let w = workdir_for(tmp.path());
    build_image(
        &w.images_dir(),
        "through",
        vec![
            Layer::new(vec![
                Entry::Dir("inner"),
                Entry::Symlink {
                    link: "s",
                    target: "inner",
                },
            ]),
            Layer::new(vec![Entry::File("s/leaf", b"leaf".to_vec(), 0o644)]),
        ],
        None,
    );
    let report = w.rebuild("through", &Limits::default()).unwrap();
    assert_eq!(
        read_rootfs(&w, "through", "s/leaf").as_deref(),
        Some(&b"leaf"[..])
    );
    let s = report.files.iter().find(|f| f.path == "s").unwrap();
    assert_eq!(s.kind, "directory");
    assert_eq!(s.layer_index, 1);
    // No symlink remains at s, so nothing can escape via it.
    assert!(w
        .published_root("through")
        .unwrap()
        .unwrap()
        .join("s")
        .symlink_metadata()
        .unwrap()
        .is_dir());
}

#[test]
fn rejects_too_many_entries() {
    let tmp = tmp_workdir();
    let w = workdir_for(tmp.path());
    let mut entries = vec![Entry::Dir("many")];
    for i in 0..150u32 {
        entries.push(Entry::File(
            Box::leak(format!("many/f{i}").into_boxed_str()),
            vec![1],
            0o644,
        ));
    }
    build_image(
        &w.images_dir(),
        "many",
        vec![Layer::new(entries).plain()],
        None,
    );
    let e = w.rebuild("many", &tight_limits()).unwrap_err();
    assert!(matches!(e, AppError::Limit(_)), "got {e:?}");
}

// --------------------------------------------------------------------------
// 8. Hardlink that targets outside / non-existent target
// --------------------------------------------------------------------------

#[test]
fn rejects_escaping_hardlink() {
    let tmp = tmp_workdir();
    let w = workdir_for(tmp.path());
    build_image(
        &w.images_dir(),
        "hln",
        vec![Layer::new(vec![
            Entry::File("real", b"x".to_vec(), 0o644),
            Entry::Raw(RawEntry {
                name: "alias".to_string(),
                data: vec![],
                mode: 0o644,
                typeflag: b'1',
                linkname: Some("../../etc/passwd".to_string()),
            }),
        ])],
        None,
    );
    let e = w.rebuild("hln", &Limits::default()).unwrap_err();
    assert!(matches!(e, AppError::RejectedEntry { .. }), "got {e}");
}

#[test]
fn accepts_ordered_hardlink_and_shares_content() {
    let tmp = tmp_workdir();
    let w = workdir_for(tmp.path());
    build_image(
        &w.images_dir(),
        "hlnok",
        vec![Layer::new(vec![
            Entry::File("orig", b"payload".to_vec(), 0o644),
            Entry::Hardlink {
                link: "alias",
                target: "orig",
            },
        ])],
        None,
    );
    let report = w.rebuild("hlnok", &Limits::default()).unwrap();
    assert_eq!(
        read_rootfs(&w, "hlnok", "alias").as_deref(),
        Some(&b"payload"[..])
    );
    let orig = report.files.iter().find(|f| f.path == "orig").unwrap();
    let alias = report.files.iter().find(|f| f.path == "alias").unwrap();
    assert_eq!(orig.content_sha256, alias.content_sha256);
}

// --------------------------------------------------------------------------
// 9. Failed rebuild never publishes partial root; previous good root stands
// --------------------------------------------------------------------------

#[test]
fn failed_rebuild_keeps_previous_good_root() {
    let tmp = tmp_workdir();
    let w = workdir_for(tmp.path());
    build_image(
        &w.images_dir(),
        "persist",
        vec![Layer::new(vec![Entry::File("ok", b"1".to_vec(), 0o644)])],
        None,
    );
    let first = w.rebuild("persist", &Limits::default()).unwrap();

    // Now mutate the fixture to a bad layer (tampered blob).
    let img_root = w.images_dir().join("persist");
    let blob = img_root
        .join("blobs")
        .join("sha256")
        .join(first.layers[0].replace("sha256:", ""));
    std::fs::write(&blob, b"corrupt bytes").unwrap();

    let err = w.rebuild("persist", &Limits::default()).unwrap_err();
    assert!(
        matches!(err, AppError::DigestMismatch { .. }),
        "got {err:?}"
    );

    // Previous root is still published and intact.
    let root = w.published_root("persist").unwrap().unwrap();
    assert_eq!(std::fs::read(root.join("ok")).unwrap(), b"1");
}

// --------------------------------------------------------------------------
// 10. Plain (uncompressed) tar layer also verifies by digest
// --------------------------------------------------------------------------

#[test]
fn plain_tar_layer_works() {
    let tmp = tmp_workdir();
    let w = workdir_for(tmp.path());
    build_image(
        &w.images_dir(),
        "plain",
        vec![Layer::new(vec![Entry::File("f", b"plain".to_vec(), 0o644)]).plain()],
        None,
    );
    let report = w.rebuild("plain", &Limits::default()).unwrap();
    assert_eq!(report.layer_count, 1);
    assert_eq!(
        read_rootfs(&w, "plain", "f").as_deref(),
        Some(&b"plain"[..])
    );
}

// --------------------------------------------------------------------------
// 11. setuid/setgid bits are stripped on extraction
// --------------------------------------------------------------------------

#[test]
fn strips_setuid_bits() {
    let tmp = tmp_workdir();
    let w = workdir_for(tmp.path());
    build_image(
        &w.images_dir(),
        "setuid",
        vec![Layer::new(vec![Entry::File("x", b"x".to_vec(), 0o4755)])],
        None,
    );
    let report = w.rebuild("setuid", &Limits::default()).unwrap();
    let f = report.files.iter().find(|f| f.path == "x").unwrap();
    assert_eq!(f.mode, "0755");
}
