#![allow(clippy::field_reassign_with_default)]
//! Resource-limit ("过量解压") protection tests.

mod common;

use common::Env;
use fixture_maker::{LayerSpec, RawEntry as E};
use oci_unpack::Limits;

#[test]
fn rejects_decompression_bomb_via_payload_limit() {
    let env = Env::new();
    // ~4 MiB of highly compressible zeroes gzips to a few KB: a classic zip
    // bomb shape that stays well under the compressed-size cap.
    let big = vec![0u8; 4 * 1024 * 1024];
    env.make("bomb", &[LayerSpec::gzip(vec![E::file("zeros.bin", big)])]);

    let mut limits = Limits::default();
    limits.max_single_file_bytes = 1024 * 1024;
    limits.max_file_payload_bytes = 1024 * 1024;

    let err = env
        .rebuild_with("bomb", limits)
        .expect_err("should be rejected");
    assert_eq!(err.code(), "limit_exceeded", "got: {err}");
    assert!(!env.published("bomb"));
}

#[test]
fn rejects_too_many_entries() {
    let env = Env::new();
    let mut entries = Vec::new();
    for i in 0..500u32 {
        entries.push(E::file(format!("f{i}"), vec![i as u8; 1]));
    }
    env.make("many", &[LayerSpec::tar(entries)]);

    let mut limits = Limits::default();
    limits.max_entries = 100;
    let err = env.rebuild_with("many", limits).expect_err("rejected");
    assert_eq!(err.code(), "limit_exceeded", "got: {err}");
    assert!(!env.published("many"));
}

#[test]
fn rejects_compressed_size_over_cap() {
    let env = Env::new();
    // Random-ish bytes do not compress, so the blob itself is large.
    let mut data = Vec::with_capacity(200_000);
    let mut x: u32 = 0x1234_5678;
    while data.len() < 200_000 {
        x ^= x << 13;
        x ^= x >> 17;
        x ^= x << 5;
        data.extend_from_slice(&x.to_le_bytes());
    }
    env.make("big", &[LayerSpec::gzip(vec![E::file("rnd", data)])]);

    let mut limits = Limits::default();
    limits.max_compressed_bytes = 1024; // 1 KiB hard cap
    let err = env.rebuild_with("big", limits).expect_err("rejected");
    assert_eq!(err.code(), "limit_exceeded", "got: {err}");
}

#[test]
fn normal_size_within_limits_succeeds() {
    let env = Env::new();
    env.make(
        "ok",
        &[LayerSpec::gzip(vec![E::file("a", b"hello".to_vec())])],
    );
    env.rebuild("ok").expect("small image rebuilds fine");
}
