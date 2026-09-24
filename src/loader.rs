//! Fixture loader: populates the store from a local directory.
//!
//! Layout:
//! ```text
//! <fixtures-dir>/
//!   <repository-name>/
//!     fixture.json
//! ```
//! `fixture.json` shape:
//! ```json
//! {
//!   "blobs": [
//!     { "json": { "...canonical OCI index/manifest object..." } },
//!     { "rawBase64": "aGVsbG8=", "mediaType": "application/x.example" }
//!   ],
//!   "tags": {
//!     "1.0": "sha256:...."
//!   }
//! }
//! ```
//! JSON blobs are re-serialized canonically (compact form of the parsed
//! value) and inserted under their real sha256 digest, so fixtures never
//! hard-code digests.

use std::path::{Path, PathBuf};

use serde::Deserialize;
use serde_json::Value;

use crate::digest::Digest;
use crate::error::{ApiError, ApiResult};
use crate::model::BlobKind;
use crate::store::{Blob, Store};

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct FixtureFile {
    #[serde(default)]
    blobs: Vec<FixtureBlob>,
    #[serde(default)]
    tags: std::collections::BTreeMap<String, String>,
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum FixtureBlob {
    Json {
        json: Value,
    },
    Raw {
        #[serde(rename = "rawBase64")]
        raw_base64: String,
    },
}

/// Load every `<dir>/*/fixture.json`. Returns loaded repository names.
pub fn load_dir(store: &mut Store, dir: &Path) -> ApiResult<Vec<String>> {
    let mut loaded = Vec::new();
    if !dir.exists() {
        return Ok(loaded);
    }
    let mut entries: Vec<PathBuf> = std::fs::read_dir(dir)
        .map_err(|e| ApiError::NotFound(format!("cannot read fixtures dir: {e}")))?
        .map(|e| e.map_err(io_err).map(|e| e.path()))
        .collect::<ApiResult<Vec<PathBuf>>>()?;
    entries.sort();

    for path in entries {
        let fixture_path = path.join("fixture.json");
        if path.is_dir() && fixture_path.exists() {
            let repo = path
                .file_name()
                .map(|n| n.to_string_lossy().to_string())
                .unwrap_or_default();
            load_one(store, &repo, &fixture_path)?;
            loaded.push(repo);
        }
    }
    loaded.sort();
    Ok(loaded)
}

fn load_one(store: &mut Store, repo: &str, path: &Path) -> ApiResult<()> {
    let text = std::fs::read_to_string(path)
        .map_err(|e| ApiError::NotFound(format!("read {}: {e}", path.display())))?;
    let fixture: FixtureFile = serde_json::from_str(&text)
        .map_err(|e| ApiError::BadRequest(format!("parse {}: {e}", path.display())))?;

    for (i, blob) in fixture.blobs.into_iter().enumerate() {
        let bytes = match blob {
            FixtureBlob::Json { json } => serde_json::to_vec(&json)
                .map_err(|e| ApiError::BadRequest(format!("blob {i}: {e}")))?,
            FixtureBlob::Raw { raw_base64 } => base64_decode(&raw_base64)
                .map_err(|e| ApiError::BadRequest(format!("blob {i}: bad base64: {e}")))?,
        };
        let digest = Digest::sha256(&bytes).to_string();
        let kind = BlobKind::classify(media_type(&bytes), &bytes)
            .map_err(|m| ApiError::BadRequest(format!("{} blob {i}: {m}", path.display())))?;
        store
            .repo_mut(repo)
            .blobs
            .insert(digest, Blob { bytes, kind });
    }

    for (tag, digest) in fixture.tags {
        crate::reference::validate_tag(&tag)?;
        Digest::parse(&digest).map_err(|e| ApiError::BadRequest(format!("tag {tag}: {}", e.0)))?;
        if !store
            .repo(repo)
            .is_ok_and(|r| r.blobs.contains_key(&digest))
        {
            return Err(ApiError::BadRequest(format!(
                "{}: tag {tag} points at unknown digest {digest}",
                path.display()
            )));
        }
        store.repo_mut(repo).tags.insert(tag, digest);
    }
    Ok(())
}

fn media_type(bytes: &[u8]) -> Option<&'static str> {
    crate::model::known_media_type(bytes)
}

fn io_err(e: std::io::Error) -> ApiError {
    ApiError::BadRequest(format!("fixture I/O error: {e}"))
}

/// Minimal standard-alphabet base64 decoder (padding optional).
pub fn base64_decode(input: &str) -> Result<Vec<u8>, String> {
    let table = |c: u8| -> Result<u8, String> {
        Ok(match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => return Err(format!("invalid character {:?}", c as char)),
        })
    };
    let cleaned: Vec<u8> = input
        .bytes()
        .filter(|b| !matches!(b, b'\n' | b'\r' | b' ' | b'=' | b'\t'))
        .collect();
    if cleaned.len() % 4 == 1 {
        return Err("invalid length".into());
    }
    let mut out = Vec::with_capacity(cleaned.len() * 3 / 4);
    for chunk in cleaned.chunks(4) {
        let mut vals = [0u8; 4];
        for (i, &c) in chunk.iter().enumerate() {
            vals[i] = table(c)?;
        }
        out.push((vals[0] << 2) | (vals[1] >> 4));
        if chunk.len() > 2 {
            out.push((vals[1] << 4) | (vals[2] >> 2));
        }
        if chunk.len() > 3 {
            out.push((vals[2] << 6) | vals[3]);
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::base64_decode;

    #[test]
    fn base64_roundtrip() {
        assert_eq!(base64_decode("aGVsbG8=").unwrap(), b"hello");
        assert_eq!(base64_decode("Zm9vYmFy").unwrap(), b"foobar");
        assert_eq!(base64_decode("YWJjZGU=").unwrap(), b"abcde");
    }
}
