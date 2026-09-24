//! Shared helpers for integration tests: build stores from JSON objects.

#![allow(dead_code)]

use manifest_selector::digest::Digest;
use manifest_selector::model::BlobKind;
use manifest_selector::store::{Blob, Store};
use serde_json::Value;

pub fn canonical(value: &Value) -> Vec<u8> {
    // serde_json sorts keys in BTreeMap-backed Values by default.
    serde_json::to_vec(value).unwrap()
}

pub fn insert(store: &mut Store, repo: &str, value: Value) -> String {
    let bytes = canonical(&value);
    let digest = Digest::sha256(&bytes).to_string();
    let kind =
        BlobKind::classify(manifest_selector::model::known_media_type(&bytes), &bytes).unwrap();
    store
        .repo_mut(repo)
        .blobs
        .insert(digest.clone(), Blob { bytes, kind });
    digest
}

/// Insert bytes under an arbitrary claimed digest (simulated tampering).
pub fn insert_claimed(store: &mut Store, repo: &str, value: &Value, claimed: &str) -> String {
    let bytes = canonical(value);
    let kind =
        BlobKind::classify(manifest_selector::model::known_media_type(&bytes), &bytes).unwrap();
    store
        .repo_mut(repo)
        .blobs
        .insert(claimed.to_string(), Blob { bytes, kind });
    claimed.to_string()
}

pub fn tag(store: &mut Store, repo: &str, name: &str, digest: &str) {
    store
        .put_tag(repo, name.to_string(), digest.to_string())
        .unwrap();
}

pub const OCI_INDEX: &str = "application/vnd.oci.image.index.v1+json";
pub const OCI_MANIFEST: &str = "application/vnd.oci.image.manifest.v1+json";
pub const OCI_CONFIG: &str = "application/vnd.oci.image.config.v1+json";

pub fn manifest_leaf(id: &str) -> Value {
    // `id` genuinely enters the content so different ids hash differently.
    let config_digest = Digest::sha256(format!("config-content-of:{id}").as_bytes()).to_string();
    serde_json::json!({
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST,
        "config": {
            "mediaType": OCI_CONFIG,
            "digest": config_digest,
            "size": 42 + id.len() as u64,
        },
        "layers": [],
    })
}

pub fn descriptor(leaf: &Value, platform: Value) -> Value {
    let bytes = canonical(leaf);
    serde_json::json!({
        "mediaType": OCI_MANIFEST,
        "digest": Digest::sha256(&bytes).to_string(),
        "size": bytes.len(),
        "platform": platform,
    })
}

pub fn index_of(children: Vec<Value>) -> Value {
    serde_json::json!({
        "schemaVersion": 2,
        "mediaType": OCI_INDEX,
        "manifests": children,
    })
}

pub fn index_descriptor(target_digest: &str, platform: Value, target_bytes_len: usize) -> Value {
    serde_json::json!({
        "mediaType": OCI_INDEX,
        "digest": target_digest,
        "size": target_bytes_len,
        "platform": platform,
    })
}

pub fn platform(os: &str, arch: &str, variant: Option<&str>) -> Value {
    let mut p = serde_json::json!({ "os": os, "architecture": arch });
    if let Some(v) = variant {
        p["variant"] = Value::String(v.to_string());
    }
    p
}

pub fn request(
    os: &str,
    arch: &str,
    variant: Option<&str>,
) -> manifest_selector::select::PlatformRequest {
    manifest_selector::select::PlatformRequest {
        os: Some(os.to_string()),
        architecture: Some(arch.to_string()),
        variant: variant.map(str::to_string),
        os_version: None,
        os_features: vec![],
    }
}

pub fn select(
    store: &Store,
    repo: &str,
    tagname: &str,
    req: &manifest_selector::select::PlatformRequest,
) -> Result<manifest_selector::select::Selection, manifest_selector::error::ApiError> {
    let reference = manifest_selector::reference::Ref::parse(&format!("{repo}:{tagname}")).unwrap();
    manifest_selector::select::select(store, &reference, req)
}
