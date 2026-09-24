//! Generate the example OCI fixtures shipped under `fixtures/`.
//!
//! Usage:
//!   fixture-maker <output-dir>
//!
//! Produces both benign images and deliberately malicious ones so the
//! acceptance flow exercises success and every rejection path.

use fixture_maker::{build_image, evil, Compression, LayerSpec, RawEntry as E};

fn main() -> anyhow::Result<()> {
    let out = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "fixtures".to_string());
    let root = std::path::Path::new(&out);
    std::fs::create_dir_all(root)?;

    // 1. delete-then-create + multi-layer override (benign, 3 layers)
    build_image(
        root.join("delete-recreate"),
        &[
            LayerSpec::gzip(vec![
                E::dir("app"),
                E::file("app/version.txt", b"layer0\n".to_vec()),
                E::file("app/will_be_deleted", b"old\n".to_vec()),
                E::dir("app/data"),
                E::file("app/data/a.cfg", b"a=1\n".to_vec()),
            ]),
            LayerSpec::gzip(vec![
                // delete a file, then recreate it with new content
                E::whiteout("app", "will_be_deleted"),
                E::file("app/will_be_deleted", b"new after whiteout\n".to_vec()),
                // overwrite the same path from a later layer
                E::file("app/version.txt", b"layer1\n".to_vec()),
            ]),
            LayerSpec::gzip(vec![E::file("app/version.txt", b"layer2-final\n".to_vec())]),
        ],
    )?;

    // 2. parent-directory shadowing via opaque dir (benign)
    build_image(
        root.join("opaque-shadow"),
        &[
            LayerSpec::gzip(vec![
                E::dir("etc"),
                E::file("etc/base.conf", b"base\n".to_vec()),
                E::dir("etc/app"),
                E::file("etc/app/old.cfg", b"old\n".to_vec()),
                E::file("etc/app/keep-base", b"keep\n".to_vec()),
            ]),
            LayerSpec::gzip(vec![
                // opaque: everything below etc/app from lower layers is hidden
                E::dir("etc/app"),
                E::opaque("etc/app"),
                E::file("etc/app/new.cfg", b"new\n".to_vec()),
            ]),
        ],
    )?;

    // 3. escaping symlink (must be rejected)
    build_image(
        root.join("link-escape"),
        &[LayerSpec::gzip(vec![
            E::dir("app"),
            evil::symlink_dotdot_deep(),
            // try writing through the escaping link from the SAME layer
            E::file("a/b/link/inside", b"x".to_vec()),
        ])],
    )?;

    // 4. absolute symlink (must be rejected)
    build_image(
        root.join("link-absolute"),
        &[LayerSpec::gzip(vec![E::symlink("host", "/etc")])],
    )?;

    // 5. path traversal member (must be rejected)
    build_image(
        root.join("path-traversal"),
        &[LayerSpec::gzip(vec![
            E::file("safe.txt", b"ok\n".to_vec()),
            evil::traversal_escape(),
        ])],
    )?;

    // 6. absolute path member (must be rejected)
    build_image(
        root.join("absolute-path"),
        &[LayerSpec::gzip(vec![evil::absolute_path()])],
    )?;

    // 7. device file (must be rejected)
    build_image(
        root.join("device-file"),
        &[LayerSpec::gzip(vec![
            E::file("ok.txt", b"ok\n".to_vec()),
            evil::device(),
        ])],
    )?;

    // 8. corrupt gzip (truncated) in the second layer
    build_image(
        root.join("corrupt-gzip"),
        &[
            LayerSpec::gzip(vec![E::file("base.txt", b"base\n".to_vec())]),
            LayerSpec {
                entries: vec![E::file("x.txt", b"x".to_vec())],
                compression: Compression::GzipTruncated,
                force_descriptor_digest: None,
                force_diff_id: None,
            },
        ],
    )?;

    // 9. gzip that inflates to non-tar garbage
    build_image(
        root.join("garbage-gzip"),
        &[LayerSpec {
            entries: vec![],
            compression: Compression::GarbageGzip,
            force_descriptor_digest: None,
            force_diff_id: None,
        }],
    )?;

    // 10. digest tampering: descriptor claims a wrong digest
    build_image(
        root.join("digest-tampered"),
        &[LayerSpec {
            entries: vec![E::file("ok.txt", b"ok\n".to_vec())],
            compression: Compression::Gzip,
            force_descriptor_digest: Some(
                "sha256:0000000000000000000000000000000000000000000000000000000000000000".into(),
            ),
            force_diff_id: None,
        }],
    )?;

    // 10b. diff_id tampering: valid gzip blob, config lies about uncompressed
    // tar digest (must be rejected at the uncompressed verification step).
    build_image(
        root.join("diffid-tampered"),
        &[LayerSpec {
            entries: vec![E::file("ok.txt", b"ok\n".to_vec())],
            compression: Compression::Gzip,
            force_descriptor_digest: None,
            force_diff_id: Some(
                "sha256:1111111111111111111111111111111111111111111111111111111111111111".into(),
            ),
        }],
    )?;

    // 11. hardlinks + symlink whitelist benign image
    build_image(
        root.join("links-and-hardlinks"),
        &[LayerSpec::gzip(vec![
            E::dir("bin"),
            E::file_mode("bin/original", 0o755, b"#!/bin/sh\necho hi\n".to_vec()),
            E::hardlink("bin/alias", "bin/original"),
            E::symlink("bin/current", "original"),
        ])],
    )?;

    // 12. benign plain tar (uncompressed)
    build_image(
        root.join("plain-tar"),
        &[LayerSpec::tar(vec![E::file(
            "hello.txt",
            b"hello uncompressed\n".to_vec(),
        )])],
    )?;

    println!("fixtures written to {out}");
    Ok(())
}
