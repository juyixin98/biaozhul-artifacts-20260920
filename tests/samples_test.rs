//! Sample-corpus driven tests.
//!
//! Every file under `samples/` is parsed whole and, more importantly, sliced
//! at every byte position. `samples.json` declares whether the sample must be
//! accepted or the exact `ErrorKind` code it must produce.

mod common;

use common::*;
use http_framing::parse_all;

fn load_manifest() -> Vec<sample::Entry> {
    sample::read_manifest()
}

mod sample {
    use std::path::PathBuf;

    #[derive(Debug, Clone)]
    pub struct Entry {
        pub file: String,
        pub bytes: i64,
        pub expected: String,
        pub error: Option<String>,
        #[allow(dead_code)]
        pub notes: String,
    }

    pub fn samples_dir() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("samples")
    }

    /// Minimal manifest parser tailored to tools/gen_samples.py output.
    pub fn read_manifest() -> Vec<Entry> {
        let text = std::fs::read_to_string(samples_dir().join("samples.json")).unwrap();
        let mut entries = Vec::new();
        for block in text.split('{').skip(1) {
            let block = block.split('}').next().unwrap();
            let get = |key: &str| -> Option<String> {
                let pat = format!("\"{key}\":");
                let start = block.find(&pat)? + pat.len();
                let rest = block[start..].trim_start();
                if let Some(rest) = rest.strip_prefix('"') {
                    let end = rest.find('"')?;
                    Some(rest[..end].to_string())
                } else {
                    let end = rest.find([',', '}']).unwrap_or(rest.len());
                    Some(rest[..end].trim().to_string())
                }
            };
            entries.push(Entry {
                file: get("file").unwrap(),
                bytes: get("bytes").and_then(|s| s.parse().ok()).unwrap(),
                expected: get("expected").unwrap(),
                error: get("error").filter(|s| s != "null"),
                notes: get("notes").unwrap_or_default(),
            });
        }
        entries
    }
}

#[test]
fn all_samples_match_manifest_whole() {
    for e in load_manifest() {
        let data = std::fs::read(sample::samples_dir().join(&e.file)).unwrap();
        assert_eq!(data.len() as i64, e.bytes, "sample {}", e.file);
        let result = parse_all(&data);
        match e.expected.as_str() {
            "accepted" => {
                assert!(
                    result.error.is_none(),
                    "{} unexpectedly rejected: {:?}",
                    e.file,
                    result.error
                );
                assert!(!result.frames.is_empty(), "{} produced no frame", e.file);
            }
            "rejected" => {
                let err = result
                    .error
                    .unwrap_or_else(|| panic!("{} should be rejected", e.file));
                assert_eq!(
                    Some(err.kind.code()),
                    e.error.as_deref(),
                    "{} wrong error kind",
                    e.file
                );
            }
            other => panic!("manifest bug: {other}"),
        }
    }
}

#[test]
fn every_sample_is_segmentation_stable() {
    for e in load_manifest() {
        let data = std::fs::read(sample::samples_dir().join(&e.file)).unwrap();
        let expected = reference_trace(&data);
        // Exhaustive single split for every sample (all small).
        for cut in 0..=data.len() {
            let got = trace_with_cuts(&data, &[cut, data.len()]);
            assert_eq!(got, expected, "sample {} split at {}", e.file, cut);
        }
        // byte-by-byte
        let cuts: Vec<usize> = (1..=data.len()).collect();
        assert_eq!(
            trace_with_cuts(&data, &cuts),
            expected,
            "sample {} byte-by-byte",
            e.file
        );
    }
}
