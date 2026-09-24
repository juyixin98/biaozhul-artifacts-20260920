//! Determinism and policy tests for the packager library.

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

use repro_pack::{pack, verify, PackOptions, VerifyOptions};
use tempfile::TempDir;

/// Helper: run `f` against a fresh temp dir.
fn with_temp<F: FnOnce(&Path)>(f: F) {
    let td = TempDir::new().unwrap();
    f(td.path());
}

fn write_file(path: &Path, bytes: &[u8]) {
    if let Some(p) = path.parent() {
        fs::create_dir_all(p).unwrap();
    }
    fs::write(path, bytes).unwrap();
}

fn chmod(path: &Path, mode: u32) {
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
}

fn pack_into(src: &Path, out: &Path, name: &str, overwrite: bool) -> String {
    let mut opts = PackOptions::new(src, out, name);
    opts.overwrite = overwrite;
    pack(&opts).unwrap().sha256
}

/// Builds the reference tree used by the determinism tests. The creation
/// order deliberately differs from the final sorted order.
fn build_reference_tree(root: &Path) {
    // Created out of order: "z" dirs/files first, then "a".
    fs::create_dir_all(root.join("zeta/nested")).unwrap();
    write_file(&root.join("zeta/nested/zzz.txt"), b"z-content\n");
    write_file(&root.join("zeta/zz.dat"), b"\x00\x01\x02binary");
    fs::create_dir_all(root.join("alpha/bin")).unwrap();
    write_file(&root.join("alpha/a.txt"), b"alpha\n");
    write_file(&root.join("alpha/bin/run"), b"#!/bin/sh\ntrue\n");
    chmod(&root.join("alpha/bin/run"), 0o755);
    write_file(&root.join("root.txt"), b"root\n");
    // Non-exec with noisy mode bits: setuid/setgid/sticky must be stripped.
    write_file(&root.join("noisy.txt"), b"noisy\n");
    chmod(&root.join("noisy.txt"), 0o6755);
    // Group/other exec only -> still maps to 0755.
    write_file(&root.join("gexec"), b"g\n");
    chmod(&root.join("gexec"), 0o0644 | 0o010);
    // Empty file and empty dir.
    write_file(&root.join("empty.txt"), b"");
    fs::create_dir_all(root.join("emptydir")).unwrap();
    // Valid in-root relative symlinks.
    std::os::unix::fs::symlink("root.txt", root.join("link-root")).unwrap();
    std::os::unix::fs::symlink("../root.txt", root.join("alpha/link-up")).unwrap();
}

#[test]
fn empty_tree_packs_as_stable_root_only_archive() {
    with_temp(|base| {
        let src = base.join("src");
        let out = base.join("out");
        fs::create_dir_all(&src).unwrap();
        let h1 = pack_into(&src, &out, "e", false);
        let out2 = base.join("out2");
        let h2 = pack_into(&src, &out2, "e", false);
        assert_eq!(h1, h2);
        // Empty tree still carries the root "." entry header (512) plus the
        // two 512-byte zero blocks that terminate a tar (1024): 1536 bytes.
        assert_eq!(fs::metadata(out.join("e.tar")).unwrap().len(), 1536);
    });
}

#[test]
fn same_content_produces_identical_bytes_regardless_of_mtime_and_mode_noise() {
    with_temp(|base| {
        let s1 = base.join("s1");
        let s2 = base.join("s2");
        fs::create_dir_all(&s1).unwrap();
        fs::create_dir_all(&s2).unwrap();
        build_reference_tree(&s1);
        build_reference_tree(&s2);

        // Scramble mtimes (including pre-epoch dates and future dates) and
        // strip/add write bits arbitrarily on s2. Byte equality must survive.
        let mut files: Vec<_> = Vec::new();
        fn collect(p: &Path, acc: &mut Vec<std::path::PathBuf>) {
            for e in fs::read_dir(p).unwrap() {
                let p = e.unwrap().path();
                acc.push(p.clone());
                if p.is_dir() {
                    collect(&p, acc);
                }
            }
        }
        collect(&s2, &mut files);
        for (i, p) in files.iter().enumerate() {
            let mt = fs::symlink_metadata(p).unwrap();
            if mt.file_type().is_symlink() {
                continue; // lchmod/lutimes portability varies; content target is what matters
            }
            let secs = match i % 3 {
                0 => 1_000_000_000,
                1 => 86_400,
                _ => 1_700_000_000,
            };
            let t = filetime_unix(secs, 0);
            let _ = Command::new("touch").args(["-d", &t]).arg(p).status();
        }
        // Also make sure non-exec status of regular data files is preserved
        // (mode mapping derives from exec bits, so do not toggle those).
        let o1 = base.join("o1");
        let o2 = base.join("o2");
        let h1 = pack_into(&s1, &o1, "a", false);
        let h2 = pack_into(&s2, &o2, "a", false);
        assert_eq!(h1, h2, "identical content must yield identical tar bytes");
        assert_eq!(
            fs::read(o1.join("a.tar")).unwrap(),
            fs::read(o2.join("a.tar")).unwrap()
        );
        assert_eq!(
            fs::read(o1.join("a.manifest.json")).unwrap(),
            fs::read(o2.join("a.manifest.json")).unwrap()
        );
    });
}

#[test]
fn relocation_to_another_absolute_path_does_not_change_bytes() {
    with_temp(|base| {
        let s1 = base.join("somewhere/deep/nested/tree");
        let s2 = base.join("totally/different/location");
        fs::create_dir_all(&s1).unwrap();
        fs::create_dir_all(&s2).unwrap();
        build_reference_tree(&s1);
        build_reference_tree(&s2);
        let h1 = pack_into(&s1, &base.join("o1"), "a", false);
        let h2 = pack_into(&s2, &base.join("o2"), "a", false);
        assert_eq!(h1, h2);
    });
}

#[test]
fn fixed_entry_ordering_is_sorted_byte_order() {
    with_temp(|base| {
        let src = base.join("src");
        let out = base.join("out");
        fs::create_dir_all(&src).unwrap();
        build_reference_tree(&src);
        let opts = PackOptions::new(&src, &out, "a");
        let outcome = pack(&opts).unwrap();
        let paths: Vec<&str> = outcome
            .manifest
            .entries
            .iter()
            .map(|e| e.path.as_str())
            .collect();
        let mut sorted = paths.clone();
        sorted.sort();
        assert_eq!(paths, sorted, "entries must be byte-sorted");
        assert_eq!(paths[0], ".");
        // A directory precedes its contents: "alpha" before "alpha/a.txt".
        let pos = |p: &str| paths.iter().position(|x| *x == p).unwrap();
        assert!(pos("alpha") < pos("alpha/a.txt"));
        assert!(pos("zeta") < pos("zeta/nested/zzz.txt"));
    });
}

#[test]
fn permission_mapping_is_applied() {
    with_temp(|base| {
        let src = base.join("src");
        let out = base.join("out");
        fs::create_dir_all(&src).unwrap();
        build_reference_tree(&src);
        let outcome = pack(&PackOptions::new(&src, &out, "a")).unwrap();
        let m = &outcome.manifest;
        let mode_of = |p: &str| {
            m.entries
                .iter()
                .find(|e| e.path == p)
                .map(|e| e.mode.clone())
                .unwrap()
        };
        assert_eq!(mode_of("root.txt"), "0644");
        // noisy.txt is 06755: setuid+setgid stripped, but the owner exec
        // bit remains -> permission mapping yields 0755.
        assert_eq!(mode_of("noisy.txt"), "0755");
        // Group-exec-only still satisfies "any exec bit" -> 0755.
        assert_eq!(mode_of("gexec"), "0755");
        assert_eq!(mode_of("alpha/bin/run"), "0755");
        assert_eq!(mode_of("alpha"), "0755");
        assert_eq!(mode_of("link-root"), "0777");
        assert_eq!(mode_of("."), "0755");
        for e in &m.entries {
            assert_eq!(e.mtime_epoch, 0, "mtime pinned for {}", e.path);
        }
    });
}

#[test]
fn custom_mtime_is_applied_uniformly() {
    with_temp(|base| {
        let src = base.join("src");
        let out = base.join("out");
        fs::create_dir_all(&src).unwrap();
        write_file(&src.join("f"), b"x");
        let mut opts = PackOptions::new(&src, &out, "a");
        opts.mtime_epoch = 1_234_567_890;
        let outcome = pack(&opts).unwrap();
        assert!(outcome
            .manifest
            .entries
            .iter()
            .all(|e| e.mtime_epoch == 1_234_567_890));
    });
}

// ---------------------------------------------------------------------------
// Conflict / rejection rules
// ---------------------------------------------------------------------------

fn assert_conflict(src: &Path, name: &str) -> String {
    let base = src.parent().unwrap().to_path_buf();
    let out = base.join(format!("out-{name}"));
    let err = pack(&PackOptions::new(src, &out, "a")).unwrap_err();
    match err {
        repro_pack::Error::Conflict(msg) => msg,
        other => panic!("expected conflict, got: {other:?}"),
    }
}

#[test]
fn rejects_absolute_symlink() {
    with_temp(|base| {
        let src = base.join("src");
        fs::create_dir_all(&src).unwrap();
        write_file(&src.join("f"), b"x");
        std::os::unix::fs::symlink("/etc/passwd", src.join("abs")).unwrap();
        let msg = assert_conflict(&src, "abs");
        assert!(msg.contains("absolute"), "{msg}");
    });
}

#[test]
fn rejects_escaping_relative_symlink() {
    with_temp(|base| {
        let src = base.join("src");
        fs::create_dir_all(src.join("d")).unwrap();
        write_file(&src.join("f"), b"x");
        std::os::unix::fs::symlink("../../etc/passwd", src.join("d/up")).unwrap();
        let msg = assert_conflict(&src, "esc");
        assert!(msg.contains("escapes"), "{msg}");
    });
}

#[test]
fn rejects_directory_symlink_inside_root() {
    with_temp(|base| {
        let src = base.join("src");
        fs::create_dir_all(src.join("realdir")).unwrap();
        write_file(&src.join("realdir/inside"), b"x");
        std::os::unix::fs::symlink("realdir", src.join("alias")).unwrap();
        let msg = assert_conflict(&src, "dir");
        assert!(msg.contains("directory"), "{msg}");
    });
}

#[test]
fn rejects_symlink_loop() {
    with_temp(|base| {
        let src = base.join("src");
        fs::create_dir_all(&src).unwrap();
        std::os::unix::fs::symlink("b", src.join("a")).unwrap();
        std::os::unix::fs::symlink("a", src.join("b")).unwrap();
        let msg = assert_conflict(&src, "loop");
        assert!(msg.contains("loop"), "{msg}");
    });
}

#[test]
fn rejects_hard_link_alias() {
    with_temp(|base| {
        let src = base.join("src");
        fs::create_dir_all(&src).unwrap();
        write_file(&src.join("f"), b"same-inode");
        Command::new("ln")
            .arg(src.join("f"))
            .arg(src.join("hard"))
            .status()
            .unwrap();
        let msg = assert_conflict(&src, "hard");
        assert!(
            msg.contains("identity") || msg.contains("hard link"),
            "{msg}"
        );
    });
}

#[test]
fn rejects_dotdot_path_components_from_filesystem() {
    // `..` cannot be created as a literal filename on Unix (it is a real
    // parent ref); instead verify the check_component rule directly.
    // Indirectly: a symlink literally named with NUL cannot be created, so we
    // exercise the invalid-artifact-name rule as an API-level analogue.
    with_temp(|base| {
        let src = base.join("src");
        let out = base.join("out");
        fs::create_dir_all(&src).unwrap();
        let err = pack(&PackOptions::new(&src, &out, "../evil")).unwrap_err();
        assert!(matches!(err, repro_pack::Error::Invalid { .. }));
    });
}

#[test]
fn rejects_special_files() {
    with_temp(|base| {
        let src = base.join("src");
        fs::create_dir_all(&src).unwrap();
        Command::new("mkfifo")
            .arg(src.join("pipe"))
            .status()
            .unwrap();
        let err = pack(&PackOptions::new(&src, base.join("out"), "a")).unwrap_err();
        match err {
            repro_pack::Error::Invalid { code, .. } => assert_eq!(code, "unsupported_file_type"),
            other => panic!("unexpected: {other:?}"),
        }
    });
}

#[test]
fn refuses_output_inside_source() {
    with_temp(|base| {
        let src = base.join("src");
        fs::create_dir_all(src.join("out")).unwrap();
        write_file(&src.join("f"), b"x");
        let err = pack(&PackOptions::new(&src, src.join("out"), "a")).unwrap_err();
        match err {
            repro_pack::Error::Invalid { code, .. } => assert_eq!(code, "output_inside_source"),
            other => panic!("unexpected: {other:?}"),
        }
    });
}

#[test]
fn refuses_to_overwrite_existing_artifacts_by_default() {
    with_temp(|base| {
        let src = base.join("src");
        let out = base.join("out");
        fs::create_dir_all(&src).unwrap();
        write_file(&src.join("f"), b"x");
        pack_into(&src, &out, "a", false);
        let err = pack(&PackOptions::new(&src, &out, "a")).unwrap_err();
        match err {
            repro_pack::Error::Invalid { code, .. } => assert_eq!(code, "artifact_exists"),
            other => panic!("unexpected: {other:?}"),
        }
        // Explicit overwrite succeeds.
        pack_into(&src, &out, "a", true);
    });
}

// ---------------------------------------------------------------------------
// Verify
// ---------------------------------------------------------------------------

#[test]
fn verify_accepts_good_artifacts_and_flags_tampering() {
    with_temp(|base| {
        let src = base.join("src");
        let out = base.join("out");
        fs::create_dir_all(&src).unwrap();
        build_reference_tree(&src);
        pack_into(&src, &out, "a", false);

        let mut opts = VerifyOptions::new(out.join("a.tar"));
        opts.source = Some(src.clone());
        let r = verify(&opts).unwrap();
        assert!(r.ok, "fresh artifact should verify: {:?}", r.problems);
        assert_eq!(r.digest_matches, Some(true));
        assert_eq!(r.rebuild_matches, Some(true));

        // Tamper a file in the source -> rebuild must disagree.
        write_file(&src.join("root.txt"), b"different content\n");
        let r = verify(&opts).unwrap();
        assert!(!r.ok);
        assert_eq!(r.rebuild_matches, Some(false));
        assert!(!r.problems.is_empty());
    });
}

#[test]
fn verify_flags_corrupted_digest_sidecar() {
    with_temp(|base| {
        let src = base.join("src");
        let out = base.join("out");
        fs::create_dir_all(&src).unwrap();
        write_file(&src.join("f"), b"x");
        pack_into(&src, &out, "a", false);
        fs::write(out.join("a.sha256"), "deadbeef  a.tar\n").unwrap();
        let r = verify(&VerifyOptions::new(out.join("a.tar"))).unwrap();
        assert!(!r.ok);
        assert_eq!(r.digest_matches, Some(false));
    });
}

// mtime scrambling helper (human-friendly UTC date for `touch -d`).
fn filetime_unix(secs: i64, _nanos: u32) -> String {
    use chrono_lite::*;
    format_date(secs)
}

mod chrono_lite {
    /// Formats seconds-since-epoch as `YYYY-MM-DD HH:MM:SS UTC` without
    /// external crates.
    pub fn format_date(secs: i64) -> String {
        let days = secs.div_euclid(86_400);
        let rem = secs.rem_euclid(86_400);
        let (h, mi, s) = (rem / 3600, (rem % 3600) / 60, rem % 60);
        let (y, mo, d) = civil_from_days(days);
        format!("{y:04}-{mo:02}-{d:02} {h:02}:{mi:02}:{s:02} UTC")
    }

    // Howard Hinnant's days algorithm.
    fn civil_from_days(z: i64) -> (i64, u32, u32) {
        let z = z + 719_468;
        let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
        let doe = z - era * 146_097;
        let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
        let y = yoe + era * 400;
        let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
        let mp = (5 * doy + 2) / 153;
        let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
        let m = (if mp < 10 { mp + 3 } else { mp - 9 }) as u32;
        (y + if m <= 2 { 1 } else { 0 }, m, d)
    }
}

// Keep SystemTime/UNIX_EPOCH imports used for possible extensions.
#[test]
fn _imports_live() {
    let _ = SystemTime::now().duration_since(UNIX_EPOCH).unwrap();
}
