//! Platform selection over OCI image indexes.
//!
//! Selection is a deterministic, order-independent ranking — never "first
//! descriptor in the file". Two distinct leaves that satisfy *exactly the
//! same* platform conditions make the result ambiguous and produce an error
//! listing every candidate.

use std::collections::BTreeSet;

use serde::Serialize;
use serde_json::{json, Value};

use crate::digest::Digest;
use crate::error::{ApiError, ApiResult};
use crate::model::{BlobKind, Descriptor, Index, Platform};
use crate::reference::Ref;
use crate::store::Store;

/// Platform constraints from the client.
#[derive(Debug, Clone, Default, serde::Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PlatformRequest {
    pub os: Option<String>,
    pub architecture: Option<String>,
    pub variant: Option<String>,
    #[serde(default)]
    pub os_version: Option<String>,
    #[serde(default)]
    pub os_features: Vec<String>,
}

impl PlatformRequest {
    pub fn validate(&self) -> ApiResult<()> {
        if self.os.as_deref().unwrap_or("").is_empty() {
            return Err(ApiError::BadRequest("platform.os is required".into()));
        }
        if self.architecture.as_deref().unwrap_or("").is_empty() {
            return Err(ApiError::BadRequest(
                "platform.architecture is required".into(),
            ));
        }
        Ok(())
    }
}

/// One node in the selection path returned to the client.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PathNode {
    pub digest: String,
    /// `"index"` or `"manifest"` (or `"other"`).
    pub kind: &'static str,
    /// Position of this node in the parent index's `manifests` array.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub via_child_index: Option<usize>,
    /// Platform declared on the parent descriptor that reached this node.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub platform: Option<Platform>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Candidate {
    pub digest: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub media_type: Option<String>,
    pub size: u64,
    pub platform: Option<Platform>,
    /// Root -> ... -> leaf chain of manifests.
    pub path: Vec<PathNode>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Selection {
    pub repository: String,
    pub resolved_tag: Option<String>,
    pub root_digest: String,
    pub requested: Value,
    /// `"direct"` for a tag/digest pointing straight at an image manifest,
    /// `"index"` for a leaf chosen from an index tree.
    pub match_type: &'static str,
    #[serde(flatten)]
    pub chosen: Candidate,
}

/// A collected leaf during traversal.
struct Leaf {
    descriptor: Descriptor,
    platform: Option<Platform>,
    path: Vec<PathNode>,
}

/// Tunables for selection. Production callers use [`SelectOptions::default`],
/// which always verifies content digests.
#[derive(Debug, Clone)]
pub struct SelectOptions {
    /// Re-compute and check every blob's digest while traversing.
    pub verify_digests: bool,
}

impl Default for SelectOptions {
    fn default() -> Self {
        SelectOptions {
            verify_digests: true,
        }
    }
}

/// Resolve `reference` against the store and run platform selection.
/// Digest verification is always performed.
pub fn select(store: &Store, reference: &Ref, req: &PlatformRequest) -> ApiResult<Selection> {
    select_with(store, reference, req, &SelectOptions::default())
}

/// Core entry point with explicit options (tests disable verification to
/// exercise the structurally-unreachable cycle guard).
pub fn select_with(
    store: &Store,
    reference: &Ref,
    req: &PlatformRequest,
    options: &SelectOptions,
) -> ApiResult<Selection> {
    req.validate()?;

    let root = resolve_root(store, reference)?;
    let root_digest = Digest::parse(&root).map_err(|e| ApiError::InvalidContent {
        message: format!("stored tag points at invalid digest: {}", e.0),
        at: format!("repository {:?}", reference.repository),
    })?;

    // Verify and classify the root before walking it. The kind was computed
    // at ingest time; with verification enabled, fetching re-checks digest.
    let root_blob = get_blob(store, &reference.repository, &root_digest, options)?;
    let root_kind = &root_blob.kind;

    let root_node = PathNode {
        digest: root_digest.to_string(),
        kind: kind_name(root_kind),
        via_child_index: None,
        platform: None,
    };

    let mut examined: Vec<Value> = Vec::new();

    let chosen: Candidate = match root_kind {
        BlobKind::Manifest(_) => Candidate {
            digest: root_digest.to_string(),
            media_type: media_type_of(&root_blob.bytes).map(str::to_string),
            size: root_blob.bytes.len() as u64,
            platform: None,
            path: vec![root_node],
        },
        BlobKind::Index(index) => {
            let mut leaves: Vec<Leaf> = Vec::new();
            let mut stack: Vec<String> = Vec::new();
            let root_path = vec![root_node];
            walk(
                store,
                &reference.repository,
                &root_digest,
                index,
                root_path,
                req,
                options,
                &mut stack,
                &mut leaves,
                &mut examined,
            )?;
            pick(leaves, req, &mut examined)?
        }
        BlobKind::Other => {
            return Err(ApiError::InvalidContent {
                message: "reference resolves to a blob that is neither an image index nor an image manifest".into(),
                at: root_digest.to_string(),
            });
        }
    };

    examined.sort_by(canonical_json_cmp);
    examined.dedup();

    Ok(Selection {
        repository: reference.repository.clone(),
        resolved_tag: reference.tag.clone(),
        root_digest: root_digest.to_string(),
        requested: serde_json::to_value(req).unwrap_or(json!({})),
        match_type: if chosen.path.len() == 1 {
            "direct"
        } else {
            "index"
        },
        chosen,
    })
}

fn resolve_root(store: &Store, reference: &Ref) -> ApiResult<String> {
    let tag_digest = match &reference.tag {
        Some(tag) => Some(store.resolve_tag(&reference.repository, tag)?.to_string()),
        None => None,
    };
    match (tag_digest, &reference.digest) {
        (Some(t), Some(d)) if t != d.as_str() => Err(ApiError::BadRequest(format!(
            "reference pins {} but tag {:?} resolves to {t}",
            d, reference.tag
        ))),
        (Some(t), _) => Ok(t),
        (None, Some(d)) => Ok(d.to_string()),
        (None, None) => Err(ApiError::BadRequest(
            "reference must include a tag or digest".into(),
        )),
    }
}

#[allow(clippy::too_many_arguments)]
fn walk(
    store: &Store,
    repo: &str,
    index_digest: &Digest,
    index: &Index,
    path_so_far: Vec<PathNode>,
    req: &PlatformRequest,
    options: &SelectOptions,
    stack: &mut Vec<String>,
    leaves: &mut Vec<Leaf>,
    examined: &mut Vec<Value>,
) -> ApiResult<()> {
    if stack.iter().any(|d| d == index_digest.as_str()) {
        let mut path = stack.clone();
        path.push(index_digest.to_string());
        return Err(ApiError::Cycle { path });
    }
    stack.push(index_digest.to_string());

    for (i, child) in index.manifests.iter().enumerate() {
        let child_digest = Digest::parse(&child.digest).map_err(|e| ApiError::InvalidContent {
            message: format!("child descriptor has invalid digest: {}", e.0),
            at: format!("{}#manifests[{i}]", index_digest),
        })?;

        let child_platform = child.platform();
        if let Some(p) = &child_platform {
            examined.push(serde_json::to_value(p).unwrap_or(json!({})));
        }

        // Pre-filter before fetching the blob (so a missing blob on an
        // irrelevant branch never produces a spurious error). The declared
        // descriptor media type tells us whether the child is a nested index
        // (a coarse constraint — fields the parent omits must not prune the
        // subtree) or a leaf manifest (a strict, fully-specified condition).
        let child_is_index = child.media_type.as_deref().is_some_and(|m| {
            m == crate::model::MEDIA_INDEX || m == crate::model::MEDIA_DOCKER_INDEX
        });
        if let Some(p) = &child_platform {
            let compatible = if child_is_index {
                coarse_compatible(req, p)
            } else {
                hard_match(req, p)
            };
            if !compatible {
                continue;
            }
        }

        let child_blob = get_blob(store, repo, &child_digest, options).map_err(|e| match e {
            ApiError::MissingBlob { digest, .. } => ApiError::MissingBlob {
                digest,
                at: format!("{}#manifests[{i}]", index_digest),
            },
            other => other,
        })?;
        let child_kind = &child_blob.kind;

        let child_node = PathNode {
            digest: child_digest.to_string(),
            kind: kind_name(child_kind),
            via_child_index: Some(i),
            platform: child_platform.clone(),
        };
        let mut child_path = path_so_far.clone();
        child_path.push(child_node);

        match child_kind {
            BlobKind::Manifest(_) => leaves.push(Leaf {
                descriptor: child.clone(),
                platform: child_platform,
                path: child_path,
            }),
            BlobKind::Index(nested) => walk(
                store,
                repo,
                &child_digest,
                nested,
                child_path,
                req,
                options,
                stack,
                leaves,
                examined,
            )?,
            BlobKind::Other => {
                return Err(ApiError::InvalidContent {
                    message: "index child is neither an image index nor an image manifest".into(),
                    at: child_digest.to_string(),
                });
            }
        }
    }

    stack.pop();
    Ok(())
}

/// Hard constraints: every rule must hold for a leaf to be considered at all.
fn hard_match(req: &PlatformRequest, p: &Platform) -> bool {
    // OS and architecture: exact string match.
    let arch_ok = p
        .architecture
        .as_deref()
        .is_some_and(|a| Some(a) == req.architecture.as_deref());
    let os_ok =
        p.os.as_deref()
            .is_some_and(|o| Some(o) == req.os.as_deref());

    // Variant: when the client names a variant it must match exactly
    // (arm/v5, v6, v7 and arm64/v8 are distinct platforms). When the client
    // omits it, any variant passes and ranking decides preference.
    let variant_ok = match (&req.variant, &p.variant) {
        (Some(wanted), Some(have)) => wanted == have,
        (Some(_), None) => false,
        (None, _) => true,
    };

    // osVersion: if the client names one, it must match exactly.
    let os_version_ok = match (&req.os_version, &p.os_version) {
        (Some(wanted), Some(have)) => wanted == have,
        (Some(_), None) => false,
        (None, _) => true,
    };

    // osFeatures: every requested feature must be provided by the candidate
    // (a candidate may expose a superset).
    let features_ok = req
        .os_features
        .iter()
        .all(|f| p.os_features.iter().any(|c| c == f));

    arch_ok && os_ok && variant_ok && os_version_ok && features_ok
}

/// Coarse compatibility used to prune a *nested index* subtree. A nested
/// descriptor is an aggregate/partial constraint: fields it leaves
/// unspecified must not rule out descendants that specify them. Only
/// contradictions (different arch/os, an explicitly incompatible variant
/// level, a missing hard requirement, or absent requested features) prune.
fn coarse_compatible(req: &PlatformRequest, p: &Platform) -> bool {
    if let Some(wanted_arch) = req.architecture.as_deref() {
        if let Some(have_arch) = p.architecture.as_deref() {
            if !arch_family_compatible(wanted_arch, have_arch) {
                return false;
            }
        }
    }
    if let (Some(wanted_os), Some(have_os)) = (req.os.as_deref(), p.os.as_deref()) {
        if wanted_os != have_os {
            return false;
        }
    }
    // If the parent names a variant, the requested variant must belong to
    // the same level-or-higher ARM family.
    if let (Some(wanted_variant), Some(have_variant)) =
        (req.variant.as_deref(), p.variant.as_deref())
    {
        match (variant_level(wanted_variant), variant_level(have_variant)) {
            (Some(w), Some(h)) if h > w => return false,
            _ => {}
        }
    }
    if let (Some(wanted_ver), Some(have_ver)) = (req.os_version.as_deref(), p.os_version.as_deref())
    {
        if wanted_ver != have_ver {
            return false;
        }
    }
    for f in &req.os_features {
        if !p.os_features.iter().any(|c| c == f) && !p.os_features.is_empty() {
            // An empty feature list on a parent means "unspecified"; a
            // non-empty list lacking a required feature is a contradiction.
            return false;
        }
    }
    true
}

/// `arm64` is its own family; `arm` ancestors accept arm v5/v6/v7 requests.
fn arch_family_compatible(wanted: &str, have: &str) -> bool {
    wanted == have || (wanted == "arm" && have == "arm") || (wanted == "arm64" && have == "arm64")
}

/// Preference score. Higher wins. Only computed for hard-matching leaves.
/// Components, most significant first:
/// 1. variant affinity  (explicit match > higher variant level when unspecified)
/// 2. osVersion affinity
/// 3. fewer surplus osFeatures
/// 4. variant text length (last-resort deterministic tiebreak, never the
///    deciding factor between different digests — those become ambiguous)
fn score(req: &PlatformRequest, p: &Platform) -> (i64, i64, i64, i64) {
    let variant = match (&req.variant, &p.variant) {
        (Some(wanted), Some(have)) if wanted == have => 1_000,
        (None, Some(have)) => (variant_level(have).unwrap_or(0) as i64) * 100,
        _ => 10,
    };

    let os_version = match (&req.os_version, &p.os_version) {
        (Some(w), Some(h)) if w == h => 100,
        (None, None) => 80,
        (None, Some(_)) => 50,
        _ => 0,
    };

    let surplus = p
        .os_features
        .iter()
        .filter(|f| !req.os_features.iter().any(|r| r == *f))
        .count() as i64;
    let features = 100 - surplus.min(100);

    let variant_text = p.variant.as_deref().map(str::len).unwrap_or(0) as i64;

    (variant, os_version, features, variant_text)
}

/// Map ARM variant names to an ordering level. `v8` > `v7` > `v6` > `v5`;
/// suffixed spellings (e.g. `v7l`) order just above their numeric prefix.
fn variant_level(v: &str) -> Option<u32> {
    let start = v.bytes().position(|b| b.is_ascii_digit())?;
    let digits: String = v[start..]
        .chars()
        .take_while(|c| c.is_ascii_digit())
        .collect();
    let n: u32 = digits.parse().ok()?;
    let has_suffix = start + digits.len() < v.len();
    Some(n * 10 + if has_suffix { 1 } else { 0 })
}

fn pick(
    leaves: Vec<Leaf>,
    req: &PlatformRequest,
    examined: &mut Vec<Value>,
) -> ApiResult<Candidate> {
    let matching: Vec<&Leaf> = leaves
        .iter()
        .filter(|l| l.platform.as_ref().is_some_and(|p| hard_match(req, p)))
        .collect();

    if matching.is_empty() {
        let message = format!(
            "no manifest found for os={} architecture={} variant={} osVersion={} osFeatures={:?}",
            disp(&req.os),
            disp(&req.architecture),
            disp(&req.variant),
            disp(&req.os_version),
            req.os_features
        );
        return Err(ApiError::NoMatch {
            message,
            tried: std::mem::take(examined),
        });
    }

    // Best score across all matching leaves — computed globally, never by
    // stopping at the first descriptor.
    let best_score = matching
        .iter()
        .map(|l| score(req, l.platform.as_ref().unwrap()))
        .max()
        .unwrap();

    let finalists: Vec<&&Leaf> = matching
        .iter()
        .filter(|l| score(req, l.platform.as_ref().unwrap()) == best_score)
        .collect();

    let distinct_digests: BTreeSet<&str> = finalists
        .iter()
        .map(|l| l.descriptor.digest.as_str())
        .collect();

    // Any tie on the *full preference score* between different blobs is
    // ambiguous: file order must never decide. Group by platform tuple so the
    // error can state whether it was an exact same-platform collision or two
    // indistinguishable tuples.
    if distinct_digests.len() > 1 {
        let mut candidates: Vec<Value> = finalists
            .iter()
            .map(|l| {
                json!({
                    "digest": l.descriptor.digest,
                    "mediaType": l.descriptor.media_type,
                    "size": l.descriptor.size,
                    "platform": l.platform,
                    "path": l.path,
                })
            })
            .collect();
        candidates.sort_by(canonical_json_cmp);
        candidates.dedup();
        return Err(ApiError::Ambiguous {
            message: format!(
                "{} distinct manifests match os={} architecture={} variant={} osVersion={} osFeatures={:?} with equal preference; refusing to pick by file order",
                candidates.len(),
                disp(&req.os),
                disp(&req.architecture),
                disp(&req.variant),
                disp(&req.os_version),
                req.os_features
            ),
            candidates,
        });
    }

    let winner = finalists[0];
    Ok(Candidate {
        digest: winner.descriptor.digest.clone(),
        media_type: winner.descriptor.media_type.clone(),
        size: winner.descriptor.size,
        platform: winner.platform.clone(),
        path: winner.path.clone(),
    })
}

fn kind_name(kind: &BlobKind) -> &'static str {
    match kind {
        BlobKind::Index(_) => "index",
        BlobKind::Manifest(_) => "manifest",
        BlobKind::Other => "other",
    }
}

fn media_type_of(bytes: &[u8]) -> Option<&'static str> {
    crate::model::known_media_type(bytes)
}

/// Fetch a blob, verifying its digest unless the (test-only) options disable
/// verification. Missing blobs are always reported.
fn get_blob<'s>(
    store: &'s Store,
    repo: &str,
    digest: &Digest,
    options: &SelectOptions,
) -> ApiResult<&'s crate::store::Blob> {
    if options.verify_digests {
        store.get(repo, digest)
    } else {
        let repo_obj = store.repo(repo)?;
        repo_obj
            .blobs
            .get(digest.as_str())
            .ok_or_else(|| ApiError::MissingBlob {
                digest: digest.to_string(),
                at: format!("repository {repo:?}"),
            })
    }
}

fn canonical_json_cmp(a: &Value, b: &Value) -> std::cmp::Ordering {
    let sa = serde_json::to_vec(a).unwrap_or_default();
    let sb = serde_json::to_vec(b).unwrap_or_default();
    sa.cmp(&sb)
}

/// Human-readable rendering of an optional constraint (`"<unset>"`).
fn disp(value: &Option<String>) -> String {
    value
        .as_deref()
        .map_or_else(|| "<unset>".to_string(), |s| format!("{s:?}"))
}

#[cfg(test)]
mod variant_tests {
    use super::variant_level;

    #[test]
    fn ordering() {
        assert_eq!(variant_level("v5"), Some(50));
        assert_eq!(variant_level("v6"), Some(60));
        assert_eq!(variant_level("v7"), Some(70));
        assert_eq!(variant_level("v8"), Some(80));
        assert_eq!(variant_level("v7l"), Some(71));
        assert_eq!(variant_level("xyz"), None);
    }
}
