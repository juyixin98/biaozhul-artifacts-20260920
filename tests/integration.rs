//! End-to-end acceptance tests for the streaming codec and JSON entry point.

use std::collections::BTreeMap;
use std::fs;
use std::io::Cursor;
use std::path::PathBuf;

use ecstripe::codec::{decode_stream, encode_stream, Limits, LossPattern};
use ecstripe::container::{Header, MAGIC, TAG_CHUNK, TAG_EOS};
use ecstripe::error::Error;

const MAX_CHUNK: u32 = 1 << 20;

fn unique_dir(tag: &str) -> PathBuf {
    use std::sync::atomic::{AtomicU64, Ordering};
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let n = COUNTER.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("ecstripe-it-{}-{}-{n}", std::process::id(), tag));
    fs::create_dir_all(&dir).unwrap();
    dir
}

fn codec_roundtrip(
    data: &[u8],
    k: u16,
    m: u16,
    s: u32,
    loss: &LossPattern,
    expect_hash: bool,
) -> Result<Vec<u8>, Error> {
    let mut src = Cursor::new(data.to_vec());
    let mut container = Cursor::new(Vec::new());
    encode_stream(
        &mut src,
        &mut container,
        k,
        m,
        s,
        data.len() as u64,
        true,
        Limits::default(),
    )?;
    container.set_position(0);
    let mut out = Cursor::new(Vec::new());
    decode_stream(
        &mut container,
        &mut out,
        loss,
        expect_hash,
        Limits::default(),
    )?;
    Ok(out.into_inner())
}

fn sample(len: usize, seed: u8) -> Vec<u8> {
    (0..len)
        .map(|i| seed.wrapping_mul(31).wrapping_add(i as u8 ^ (i >> 3) as u8))
        .collect()
}

#[test]
fn roundtrip_no_loss_multiple_stripes() {
    // 2 data shards x 7 bytes = 14 bytes/stripe; 50 bytes -> 4 stripes with a
    // short final stripe (50 = 3*14 + 8 => lengths 7,1).
    let data = sample(50, 0xa5);
    let got = codec_roundtrip(&data, 2, 1, 7, &LossPattern::default(), false).unwrap();
    assert_eq!(got, data);
}

#[test]
fn roundtrip_empty_input() {
    let got = codec_roundtrip(&[], 3, 2, 16, &LossPattern::default(), false).unwrap();
    assert!(got.is_empty());
}

#[test]
fn enumerated_loss_patterns_small_parameters() {
    // Exhaustive over small (k, m) and every loss mask with <= m losses,
    // including mixed data/parity loss, across several stripes. The largest
    // case (k=4, m=3, n=7) exercises every recoverable mask (popcount <= 3).
    let data = sample(77, 0x33);
    for k in 1u16..=4 {
        for m in 1u16..=3 {
            let s = 5u32;
            let n = k + m;
            let mut container = Cursor::new(Vec::new());
            let mut src = Cursor::new(data.clone());
            encode_stream(
                &mut src,
                &mut container,
                k,
                m,
                s,
                data.len() as u64,
                false,
                Limits::default(),
            )
            .unwrap();
            let bytes = container.into_inner();

            for mask in 0u32..(1u32 << n) {
                let loss = LossPattern {
                    shards: (0..n)
                        .filter(|i| mask & (1 << i) != 0)
                        .map(|i| i as u8)
                        .collect(),
                    cells: vec![],
                };
                let mut input = Cursor::new(bytes.clone());
                let mut out = Cursor::new(Vec::new());
                let result = decode_stream(&mut input, &mut out, &loss, false, Limits::default());
                if mask.count_ones() <= m as u32 {
                    // Recoverable: exact reconstruction.
                    result.unwrap_or_else(|e| panic!("k={k} m={m} mask={mask:b}: {e}"));
                    assert_eq!(out.into_inner(), data, "k={k} m={m} mask={mask:b}");
                } else {
                    // Too many shards missing: impossible, must be refused.
                    assert!(
                        matches!(
                            result,
                            Err(Error::NotEnoughShards { needed, .. }) if needed == k as usize
                        ),
                        "k={k} m={m} mask={mask:b}: expected NotEnoughShards, got {result:?}"
                    );
                }
            }
        }
    }
}

#[test]
fn too_many_missing_shards_is_rejected() {
    let data = sample(30, 0x01);
    let mut src = Cursor::new(data.clone());
    let mut container = Cursor::new(Vec::new());
    encode_stream(
        &mut src,
        &mut container,
        2,
        1,
        8,
        30,
        false,
        Limits::default(),
    )
    .unwrap();

    // Lose 2 of 3 shards -> only 1 survivor < k = 2.
    let loss = LossPattern {
        shards: vec![0, 2],
        cells: vec![],
    };
    container.set_position(0);
    let mut out = Cursor::new(Vec::new());
    let err = decode_stream(&mut container, &mut out, &loss, false, Limits::default()).unwrap_err();
    assert!(
        matches!(
            err,
            Error::NotEnoughShards {
                present: 1,
                needed: 2,
                ..
            }
        ),
        "got {err:?}"
    );
}

#[test]
fn per_stripe_cell_loss_is_supported_and_recovered() {
    let data = sample(60, 0x77); // several stripes at s=8,k=2
    let loss = LossPattern {
        shards: vec![],
        cells: vec![(0, 0), (2, 3)], // a different lost shard on two stripes
    };
    let got = codec_roundtrip(&data, 3, 2, 8, &loss, false).unwrap();
    assert_eq!(got, data);
}

fn make_container(header: &Header, body: Vec<u8>) -> Vec<u8> {
    let json = serde_json::to_vec(header).unwrap();
    let mut out = MAGIC.to_vec();
    out.extend_from_slice(&(json.len() as u32).to_be_bytes());
    out.extend_from_slice(&json);
    out.extend_from_slice(&body);
    out
}

fn chunk(stripe: u32, shard: u8, payload: &[u8]) -> Vec<u8> {
    let mut out = vec![TAG_CHUNK];
    out.extend_from_slice(&stripe.to_be_bytes());
    out.push(shard);
    out.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    out.extend_from_slice(payload);
    out
}

fn eos(count: u32) -> Vec<u8> {
    let mut out = vec![TAG_EOS];
    out.extend_from_slice(&count.to_be_bytes());
    out
}

#[test]
fn inconsistent_shard_length_is_rejected() {
    // k=2, m=1, S=8, L=16 (one full stripe). Present shard 0 with 7 bytes
    // (expected 8) and full parity shard 2.
    let header = Header::new(2, 1, 8, 16).unwrap();
    let mut body = chunk(0, 0, &[0u8; 7]);
    body.extend_from_slice(&chunk(0, 2, &[0u8; 8]));
    body.extend_from_slice(&eos(1));
    let bytes = make_container(&header, body);

    let mut input = Cursor::new(bytes);
    let mut out = Cursor::new(Vec::new());
    let err = decode_stream(
        &mut input,
        &mut out,
        &LossPattern::default(),
        false,
        Limits::default(),
    )
    .unwrap_err();
    assert!(
        matches!(
            err,
            Error::LengthMismatch {
                stripe: 0,
                shard: 0,
                got: 7,
                expected: 8
            }
        ),
        "got {err:?}"
    );
}

#[test]
fn oversized_chunk_record_is_rejected() {
    let header = Header::new(2, 1, 8, 16).unwrap();
    let mut bad = vec![TAG_CHUNK];
    bad.extend_from_slice(&0u32.to_be_bytes());
    bad.push(0);
    bad.extend_from_slice(&(MAX_CHUNK + 1).to_be_bytes());
    let bytes = make_container(&header, bad);
    let mut input = Cursor::new(bytes);
    let mut out = Cursor::new(Vec::new());
    let err = decode_stream(
        &mut input,
        &mut out,
        &LossPattern::default(),
        false,
        Limits::default(),
    )
    .unwrap_err();
    assert!(matches!(err, Error::BadRecord(_)), "got {err:?}");
}

#[test]
fn silent_corruption_decodes_wrong_without_external_check() {
    // Flip one byte in a parity shard, lose a data shard so the flipped byte
    // actually flows through reconstruction. Without expect_hash the decoder
    // succeeds but produces wrong data — demonstrating the documented limit.
    let data = sample(24, 0x42);
    let mut src = Cursor::new(data.clone());
    let mut container = Cursor::new(Vec::new());
    encode_stream(
        &mut src,
        &mut container,
        2,
        1,
        12,
        24,
        true,
        Limits::default(),
    )
    .unwrap();

    // Locate parity shard 2 payload: right after the two 12-byte data chunks.
    let mut bytes = container.into_inner();
    // magic(4)+hlen(4)+json, then chunks: tag1+4+1+4=10 header each.
    let json_len = u32::from_be_bytes(bytes[4..8].try_into().unwrap()) as usize;
    let mut p = 8 + json_len;
    let mut parity_payload = None;
    for _ in 0..3 {
        assert_eq!(bytes[p], TAG_CHUNK);
        let shard = bytes[p + 5];
        let len = u32::from_be_bytes(bytes[p + 6..p + 10].try_into().unwrap()) as usize;
        if shard == 2 {
            parity_payload = Some(p + 10);
        }
        p += 10 + len;
    }
    let at = parity_payload.unwrap();
    bytes[at] ^= 0x01;

    // Lose data shard 1 so reconstruction consumes the corrupted parity.
    let loss = LossPattern {
        shards: vec![1],
        cells: vec![],
    };
    let mut input = Cursor::new(bytes.clone());
    let mut out = Cursor::new(Vec::new());
    decode_stream(&mut input, &mut out, &loss, false, Limits::default())
        .expect("decode must succeed without external verification");
    let decoded = out.into_inner();
    assert_ne!(
        decoded, data,
        "corrupted parity must surface as wrong bytes"
    );
}

#[test]
fn external_hash_detects_silent_corruption() {
    let data = sample(24, 0x42);
    let mut src = Cursor::new(data.clone());
    let mut container = Cursor::new(Vec::new());
    encode_stream(
        &mut src,
        &mut container,
        2,
        1,
        12,
        24,
        true,
        Limits::default(),
    )
    .unwrap();
    let mut bytes = container.into_inner();
    let json_len = u32::from_be_bytes(bytes[4..8].try_into().unwrap()) as usize;
    // Flip a byte inside data shard 0 payload directly.
    let p = 8 + json_len + 10;
    bytes[p] ^= 0xff;

    let mut input = Cursor::new(bytes);
    let mut out = Cursor::new(Vec::new());
    let err = decode_stream(
        &mut input,
        &mut out,
        &LossPattern::default(),
        true,
        Limits::default(),
    )
    .unwrap_err();
    assert!(matches!(err, Error::HashMismatch { .. }), "got {err:?}");
}

#[test]
fn output_length_limit_is_enforced() {
    let data = sample(100, 0x09);
    // Default limits allow the round trip.
    assert!(codec_roundtrip(&data, 2, 1, 10, &LossPattern::default(), false).is_ok());

    // A decoder budget smaller than the header's declared length is refused
    // before any output is written.
    let mut src = Cursor::new(data.clone());
    let mut container = Cursor::new(Vec::new());
    encode_stream(
        &mut src,
        &mut container,
        2,
        1,
        10,
        100,
        false,
        Limits::default(),
    )
    .unwrap();
    container.set_position(0);
    let mut out = Cursor::new(Vec::new());
    let err = decode_stream(
        &mut container,
        &mut out,
        &LossPattern::default(),
        false,
        Limits {
            max_memory: 1 << 20,
            max_output: 50,
        },
    )
    .unwrap_err();
    assert!(
        matches!(
            err,
            Error::OutputLimitExceeded {
                limit: 50,
                attempted: 100
            }
        ),
        "got {err:?}"
    );
}

#[test]
fn encoder_enforces_output_and_memory_budgets() {
    // Input longer than the declared output budget.
    let mut src = Cursor::new(vec![0u8; 100]);
    let mut container = Cursor::new(Vec::new());
    let err = encode_stream(
        &mut src,
        &mut container,
        2,
        1,
        10,
        100,
        false,
        Limits {
            max_memory: 1 << 20,
            max_output: 50,
        },
    )
    .unwrap_err();
    assert!(matches!(
        err,
        Error::OutputLimitExceeded {
            limit: 50,
            attempted: 100
        }
    ));
}

#[test]
fn memory_limit_is_enforced() {
    let mut src = Cursor::new(vec![0u8; 100]);
    let mut container = Cursor::new(Vec::new());
    let err = encode_stream(
        &mut src,
        &mut container,
        4,
        4,
        1 << 16, // working set ~ 8 * 64KiB = 512 KiB
        100,
        false,
        Limits {
            max_memory: 1024,
            max_output: 1 << 30,
        },
    )
    .unwrap_err();
    assert!(matches!(err, Error::MemoryLimitExceeded { .. }));
}

#[test]
fn bad_magic_and_truncated_container_are_rejected() {
    let mut out = Cursor::new(Vec::new());
    let err = decode_stream(
        &mut Cursor::new(b"NOPE....".to_vec()),
        &mut out,
        &LossPattern::default(),
        false,
        Limits::default(),
    )
    .unwrap_err();
    assert!(matches!(err, Error::BadMagic));

    // Valid magic + header but records cut off before EOS.
    let header = Header::new(2, 1, 8, 16).unwrap();
    let bytes = make_container(&header, chunk(0, 0, &[0u8; 8]));
    let err = decode_stream(
        &mut Cursor::new(bytes),
        &mut out,
        &LossPattern::default(),
        false,
        Limits::default(),
    )
    .unwrap_err();
    assert!(matches!(err, Error::UnexpectedEos { .. }), "got {err:?}");
}

#[test]
fn zero_length_data_shard_in_final_stripe_is_explicitly_present() {
    // k=3, S=8 -> 24 bytes per stripe; L=17 -> final lengths 8,8,1 for
    // stripe... L=24+17: two stripes, second stripe lengths 8,8,1.
    // Choose L=25: stripe0 full (24), stripe1 data lens 1,0,0.
    let data = sample(25, 0x6c);
    let got = codec_roundtrip(&data, 3, 2, 8, &LossPattern::default(), false).unwrap();
    assert_eq!(got, data);

    // And it still recovers when the zero-length shard is "lost": loss of
    // shards 1 and 2 on the last stripe (both empty) plus parity elsewhere.
    let loss = LossPattern {
        shards: vec![],
        cells: vec![(1, 1), (1, 2)],
    };
    let got = codec_roundtrip(&data, 3, 2, 8, &loss, false).unwrap();
    assert_eq!(got, data);
}

#[test]
fn matrix_recovery_matches_direct_api_on_manual_stripe() {
    // Belt-and-braces: the direct stripe API matches an independent layout.
    let k = 3;
    let m = 2;
    let s = 32;
    let d0 = vec![1u8; s];
    let d1: Vec<u8> = (0..s).map(|i| i as u8).collect();
    let d2 = vec![0xa5u8; s];
    let data = [d0.as_slice(), d1.as_slice(), d2.as_slice()];
    let parity = ecstripe::encode_stripe(&data, m).unwrap();

    let mut present: BTreeMap<u8, Vec<u8>> = BTreeMap::new();
    present.insert(0, d0.clone());
    present.insert(2, d2.clone());
    present.insert(3, parity[0].clone()); // data shard 1 and parity shard 4 lost
    let recovered = ecstripe::recover_stripe(k, m, 0, s, &present).unwrap();
    assert_eq!(recovered[&1], d1);
    assert_eq!(recovered[&4], parity[1]);
}

// ---------------------------------------------------------------------------
// JSON control entry point
// ---------------------------------------------------------------------------

fn write_json(path: &PathBuf, value: &serde_json::Value) {
    fs::write(path, serde_json::to_vec_pretty(value).unwrap()).unwrap();
}

#[test]
fn cli_encode_decode_info_and_corrupt_via_json() {
    let dir = unique_dir("cli");
    let input = dir.join("input.bin");
    let container = dir.join("data.enc");
    let decoded = dir.join("decoded.bin");
    let corrupted = dir.join("data.corrupt.enc");
    let decoded_bad = dir.join("decoded.bad.bin");
    fs::write(&input, sample(40, 0x2b)).unwrap();

    let enc_req = dir.join("encode.json");
    write_json(
        &enc_req,
        &serde_json::json!({
            "input": input.to_str().unwrap(),
            "output": container.to_str().unwrap(),
            "data_shards": 3,
            "parity_shards": 2,
            "stripe_size": 8,
            "tag_hash": true
        }),
    );
    let resp = ecstripe_cli_dispatch(&["ecstripe", "encode", enc_req.to_str().unwrap()]);
    assert!(matches!(resp, cli::Response::Ok { .. }), "{resp:?}");

    let info_req = dir.join("info.json");
    write_json(
        &info_req,
        &serde_json::json!({ "input": container.to_str().unwrap() }),
    );
    let resp = ecstripe_cli_dispatch(&["ecstripe", "info", info_req.to_str().unwrap()]);
    match resp {
        cli::Response::Ok { data } => {
            assert_eq!(data["data_shards"], 3);
            assert_eq!(data["total_len"], 40);
            assert!(data["sha256"].is_string());
        }
        other => panic!("{other:?}"),
    }

    // Recover from losing one parity and one data shard on every stripe.
    let dec_req = dir.join("decode.json");
    write_json(
        &dec_req,
        &serde_json::json!({
            "input": container.to_str().unwrap(),
            "output": decoded.to_str().unwrap(),
            "loss": { "shards": [1, 4] },
            "expect_hash": true
        }),
    );
    let resp = ecstripe_cli_dispatch(&["ecstripe", "decode", dec_req.to_str().unwrap()]);
    assert!(matches!(resp, cli::Response::Ok { .. }), "{resp:?}");
    assert_eq!(fs::read(&decoded).unwrap(), fs::read(&input).unwrap());

    // Flip payload byte 0 of data shard 0 on stripe 0, then decode with hash
    // verification: recovery must report hash_mismatch.
    let cor_req = dir.join("corrupt.json");
    write_json(
        &cor_req,
        &serde_json::json!({
            "input": container.to_str().unwrap(),
            "output": corrupted.to_str().unwrap(),
            "flips": [{ "stripe": 0, "shard": 0, "offset": 0, "xor": 255 }]
        }),
    );
    let resp = ecstripe_cli_dispatch(&["ecstripe", "corrupt", cor_req.to_str().unwrap()]);
    assert!(matches!(resp, cli::Response::Ok { .. }), "{resp:?}");

    let bad_req = dir.join("decode_bad.json");
    write_json(
        &bad_req,
        &serde_json::json!({
            "input": corrupted.to_str().unwrap(),
            "output": decoded_bad.to_str().unwrap(),
            "expect_hash": true
        }),
    );
    let resp = ecstripe_cli_dispatch(&["ecstripe", "decode", bad_req.to_str().unwrap()]);
    match resp {
        cli::Response::Error { error } => assert_eq!(error.kind, "hash_mismatch"),
        other => panic!("expected hash_mismatch, got {other:?}"),
    }
}

#[test]
fn cli_decode_reports_too_many_missing_as_error_json() {
    let dir = unique_dir("cli-missing");
    let input = dir.join("input.bin");
    let container = dir.join("data.enc");
    let decoded = dir.join("decoded.bin");
    fs::write(&input, sample(24, 0x11)).unwrap();

    let enc_req = dir.join("encode.json");
    write_json(
        &enc_req,
        &serde_json::json!({
            "input": input.to_str().unwrap(),
            "output": container.to_str().unwrap(),
            "data_shards": 2,
            "parity_shards": 1,
            "stripe_size": 12
        }),
    );
    let resp = ecstripe_cli_dispatch(&["ecstripe", "encode", enc_req.to_str().unwrap()]);
    assert!(matches!(resp, cli::Response::Ok { .. }), "{resp:?}");

    let dec_req = dir.join("decode.json");
    write_json(
        &dec_req,
        &serde_json::json!({
            "input": container.to_str().unwrap(),
            "output": decoded.to_str().unwrap(),
            "loss": { "shards": [0, 1] }
        }),
    );
    let resp = ecstripe_cli_dispatch(&["ecstripe", "decode", dec_req.to_str().unwrap()]);
    match resp {
        cli::Response::Error { error } => {
            assert_eq!(error.kind, "not_enough_shards");
        }
        other => panic!("expected error, got {other:?}"),
    }
}

// The CLI module lives behind the binary target; expose a thin test-only
// shim through the public dispatcher path. Integration tests link the library,
// so the dispatcher is re-exposed here via a small companion in the crate.
mod cli {
    // Wrapper type mirroring the binary's response shape for assertions.
    #[derive(Debug)]
    pub enum Response {
        Ok { data: serde_json::Value },
        Error { error: ErrorDetail },
    }
    #[derive(Debug)]
    pub struct ErrorDetail {
        pub kind: String,
    }
}

fn ecstripe_cli_dispatch(args: &[&str]) -> cli::Response {
    // Drive the actual binary via CARGO_BIN_EXE when available; otherwise
    // skip. The response JSON is parsed back into the assertion shape.
    let bin = env!("CARGO_BIN_EXE_ecstripe");
    let owned: Vec<String> = args.iter().skip(1).map(|s| s.to_string()).collect();
    let out = std::process::Command::new(bin)
        .args(&owned)
        .output()
        .expect("run ecstripe binary");
    let value: serde_json::Value = serde_json::from_slice(&out.stdout).expect("JSON response");
    if value["status"] == "ok" {
        cli::Response::Ok {
            data: value["data"].clone(),
        }
    } else {
        cli::Response::Error {
            error: cli::ErrorDetail {
                kind: value["error"]["kind"].as_str().unwrap_or("").to_string(),
            },
        }
    }
}
