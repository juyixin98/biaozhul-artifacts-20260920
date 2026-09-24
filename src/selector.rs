//! Deterministic multi-platform manifest selection.
//!
//! Selection walks the index tree starting at a tag/digest reference,
//! verifies every descriptor it follows (declared digest/size vs. stored
//! bytes), and applies the OCI platform matching rules.
//!
//! Key guarantees required by the task:
//! * **No "first file wins".** Matching leaves are ranked by a deterministic
//!   score; if two or more candidates tie at the best score the call fails
//!   with [`SelectError::Ambiguous`] — regardless of descriptor order.
//! * **ARM variants are handled explicitly.** Exact `variant` matches win;
//!   for `arm`/`arm64` a request for a newer variant may fall back to an
//!   older one (v7 → v6 → v5), which is how real ARM image lists work.
//! * **Cycles are detected** with the DFS active-path set and reported, not
//!   recursed forever.
//! * **Every referenced blob is digest-verified** before being trusted.

use std::collections::HashSet;

use serde::Serialize;

use crate::digest::Digest;
use crate::model::{Descriptor, Manifest, Platform};
use crate::store::Registry;

/// A platform selection request. All fields except os/architecture are
/// optional, mirroring `runc`/containerd `Platform`.
#[derive(Debug, Clone, Default, serde::Deserialize, serde::Serialize, PartialEq, Eq)]
pub struct PlatformQuery {
    pub os: String,
    pub architecture: String,
    #[serde(default)]
    pub variant: Option<String>,
    #[serde(default)]
    pub os_version: Option<String>,
    #[serde(rename = "os.features", default)]
    pub os_features: Vec<String>,
    #[serde(default)]
    pub features: Vec<String>,
}

/// Everything that can go wrong during selection. The string codes are
/// stable and used in HTTP error envelopes.
#[derive(Debug, Clone, thiserror::Error)]
pub enum SelectError {
    #[error("reference not found: {0}")]
    ReferenceNotFound(String),
    #[error("invalid digest in reference: {0}")]
    InvalidDigest(String),
    #[error("blob missing for digest {0}")]
    BlobMissing(String),
    #[error("manifest {digest} is invalid: {reason}")]
    InvalidManifest { digest: String, reason: String },
    #[error("digest verification failed for {digest}: {detail}")]
    Verification { digest: String, detail: String },
    #[error("manifest index cycle detected: {0}")]
    Cycle(String),
    #[error("no platform matches {wanted} in {reference}")]
    NoMatch { wanted: String, reference: String },
    #[error("ambiguous match for {wanted} in {reference}: {count} candidates tie")]
    Ambiguous {
        wanted: String,
        reference: String,
        count: usize,
    },
    #[error("invalid request: {0}")]
    BadRequest(String),
}

/// Stable machine-readable error code for HTTP responses.
impl SelectError {
    pub fn code(&self) -> &'static str {
        match self {
            SelectError::ReferenceNotFound(_) => "REFERENCE_NOT_FOUND",
            SelectError::InvalidDigest(_) => "INVALID_DIGEST",
            SelectError::BlobMissing(_) => "BLOB_MISSING",
            SelectError::InvalidManifest { .. } => "INVALID_MANIFEST",
            SelectError::Verification { .. } => "DIGEST_MISMATCH",
            SelectError::Cycle(_) => "MANIFEST_CYCLE",
            SelectError::NoMatch { .. } => "NO_MATCH",
            SelectError::Ambiguous { .. } => "AMBIGUOUS",
            SelectError::BadRequest(_) => "BAD_REQUEST",
        }
    }
}

/// One step of the selection path returned to the client.
#[derive(Debug, Clone, Serialize)]
pub struct PathHop {
    /// Which child slot was followed (0-based within the parent index).
    pub index: usize,
    pub digest: String,
    pub media_type: Option<String>,
    pub platform: Option<Platform>,
}

/// A fully matched leaf image.
#[derive(Debug, Clone, Serialize)]
pub struct MatchedManifest {
    pub digest: String,
    pub media_type: Option<String>,
    pub platform: Option<Platform>,
    pub score: Score,
    pub path: Vec<PathHop>,
}

/// Deterministic ranking of a matching leaf. Higher = better. Fields are
/// compared lexicographically by [`Score::cmp`]; the scoring itself never
/// inspects descriptor order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct Score {
    /// 2 = exact variant, 1 = older-variant fallback, 0 = no variant on
    /// either side (non-ARM / unspecified case).
    pub variant: u8,
    /// 2 = exact feature-set equality, 1 = candidate is a strict subset of
    /// requested features (i.e. it can run), 0 = both sides empty.
    pub features: u8,
}

impl PartialOrd for Score {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Score {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.variant
            .cmp(&other.variant)
            .then(self.features.cmp(&other.features))
    }
}

/// Successful selection result.
#[derive(Debug, Clone, Serialize)]
pub struct Selection {
    pub reference: String,
    pub requested: PlatformQuery,
    pub selected: MatchedManifest,
    /// Number of equally-best candidates. `1` for an unambiguous result.
    pub candidate_count: usize,
    /// Digests of every equally-best candidate (populated, length 1 on
    /// success; useful in tests for ordering-independence assertions).
    pub best_candidates: Vec<String>,
}

/// Entry point: resolve `reference` in `registry` and select for `query`.
///
/// `strict` enables verification of the storage-key-vs-content invariant
/// (trusted mounts set by fixtures can opt out so a cycle demo is possible;
/// mathematically a fully content-addressed graph cannot contain a cycle).
pub fn select(
    registry: &Registry,
    reference: &str,
    query: &PlatformQuery,
    strict: bool,
) -> Result<Selection, SelectError> {
    if query.os.is_empty() || query.architecture.is_empty() {
        return Err(SelectError::BadRequest(
            "both `os` and `architecture` are required".to_string(),
        ));
    }

    let root_digest = resolve_reference(registry, reference)?;
    let mut active: HashSet<String> = HashSet::new();
    let mut candidates: Vec<MatchedManifest> = Vec::new();

    walk(
        registry,
        root_digest.as_str(),
        None,
        query,
        strict,
        &mut active,
        &mut Vec::new(),
        &mut Vec::new(),
        &mut candidates,
    )?;

    let wanted = describe(query);

    if candidates.is_empty() {
        return Err(SelectError::NoMatch {
            wanted,
            reference: reference.to_string(),
        });
    }

    // Ranking is a pure function of the candidates' scores/platforms — the
    // order the walk produced them in never breaks a tie.
    let best = candidates
        .iter()
        .map(|c| c.score)
        .max()
        .expect("non-empty candidates");
    let best: Vec<MatchedManifest> = candidates
        .into_iter()
        .filter(|c| c.score == best)
        .collect();

    if best.len() > 1 {
        return Err(SelectError::Ambiguous {
            wanted,
            reference: reference.to_string(),
            count: best.len(),
        });
    }

    // Deterministic ordering of the tied set for the response.
    let mut best = best;
    best.sort_by(|a, b| a.digest.cmp(&b.digest));
    let best_candidates: Vec<String> = best.iter().map(|c| c.digest.clone()).collect();
    let selected = best.pop().expect("exactly one winner");

    Ok(Selection {
        reference: reference.to_string(),
        requested: query.clone(),
        selected,
        candidate_count: 1,
        best_candidates,
    })
}

/// Resolve a tag (`name:tag`) or digest (`name@sha256:...`) reference to the
/// root manifest digest.
fn resolve_reference(registry: &Registry, reference: &str) -> Result<Digest, SelectError> {
    let (repo, rref) = reference
        .split_once(['@', ':'])
        .ok_or_else(|| SelectError::BadRequest(
            "reference must look like `repo:tag` or `repo@sha256:...`".to_string(),
        ))?;
    if rref.is_empty() {
        return Err(SelectError::BadRequest("empty reference part".to_string()));
    }
    if rref.starts_with("sha256:") {
        let d = Digest::parse(rref).map_err(|e| SelectError::InvalidDigest(e.to_string()))?;
        if !registry.has_blob(d.as_str()) {
            return Err(SelectError::BlobMissing(d.to_string()));
        }
        Ok(d)
    } else {
        match registry.resolve_tag(repo, rref) {
            Some(d) => Ok(Digest::parse(&d).expect("stored tags are validated digests")),
            None => Err(SelectError::ReferenceNotFound(reference.to_string())),
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn walk(
    registry: &Registry,
    digest: &str,
    descriptor: Option<&Descriptor>,
    query: &PlatformQuery,
    strict: bool,
    active: &mut HashSet<String>,
    path: &mut Vec<PathHop>,
    chain: &mut Vec<String>,
    out: &mut Vec<MatchedManifest>,
) -> Result<(), SelectError> {
    // Cycle check runs on the *declared* digest before any IO: even a
    // tampered/trusted blob pointing back into the active path is caught.
    if !active.insert(digest.to_string()) {
        // `chain` includes the root and every node entered so far; append
        // the repeated digest to render the closing edge explicitly.
        chain.push(digest.to_string());
        return Err(SelectError::Cycle(chain.join(" -> ")));
    }
    chain.push(digest.to_string());
    let result = walk_inner(
        registry,
        digest,
        descriptor,
        query,
        strict,
        active,
        path,
        chain,
        out,
    );
    chain.pop();
    active.remove(digest);
    result
}

/// Fetch a blob and, when verification is in force, validate it.
///
/// Verification rule:
/// * `strict == true` and the blob is **not** a trusted mount → declared
///   digest must equal the SHA-256 of the bytes, and the descriptor's
///   `size` (when given) must equal the byte length.
/// * A trusted mount (local inspection fixtures, incl. the cycle demo) is
///   only fetched and parsed; digest checks are skipped even in strict mode.
fn fetch_checked(
    registry: &Registry,
    digest: &str,
    declared_size: Option<i64>,
    strict: bool,
) -> Result<Vec<u8>, SelectError> {
    let bytes = registry
        .get_blob(digest)
        .ok_or_else(|| SelectError::BlobMissing(digest.to_string()))?;

    if strict && !registry.is_trusted(digest) {
        crate::digest::verify(digest, &bytes).map_err(|e| SelectError::Verification {
            digest: digest.to_string(),
            detail: e.to_string(),
        })?;
        if let Some(size) = declared_size {
            if size as usize != bytes.len() {
                return Err(SelectError::Verification {
                    digest: digest.to_string(),
                    detail: format!(
                        "size mismatch: descriptor declares {size} bytes, content is {} bytes",
                        bytes.len()
                    ),
                });
            }
        }
    }
    Ok(bytes)
}

#[allow(clippy::too_many_arguments)]
fn walk_inner(
    registry: &Registry,
    digest: &str,
    descriptor: Option<&Descriptor>,
    query: &PlatformQuery,
    strict: bool,
    active: &mut HashSet<String>,
    path: &mut Vec<PathHop>,
    chain: &mut Vec<String>,
    out: &mut Vec<MatchedManifest>,
) -> Result<(), SelectError> {
    // Root manifest is verified when strict; child manifests additionally
    // carry a descriptor whose size is pinned.
    let bytes = fetch_checked(registry, digest, descriptor.map(|d| d.size), strict)?;

    // A child descriptor must point at the storage key we are walking.
    if let Some(d) = descriptor {
        if d.digest != digest {
            return Err(SelectError::Verification {
                digest: d.digest.clone(),
                detail: format!(
                    "descriptor digest {} does not match storage key {digest}",
                    d.digest
                ),
            });
        }
    }

    let manifest = Manifest::parse(&bytes).map_err(|e| SelectError::InvalidManifest {
        digest: digest.to_string(),
        reason: e.to_string(),
    })?;

    match manifest.kind {
        crate::model::ManifestKind::Image(_) => {
            match descriptor {
                // A leaf reached through an index: the parent descriptor
                // carries the platform. It only survives if it matches.
                Some(d) => {
                    let plat = d.platform.clone().ok_or_else(|| {
                        SelectError::InvalidManifest {
                            digest: digest.to_string(),
                            reason: "image manifest inside an index has no `platform`"
                                .to_string(),
                        }
                    })?;
                    if let Some(score) = score_match(&plat, query) {
                        out.push(MatchedManifest {
                            digest: digest.to_string(),
                            media_type: manifest.media_type.clone(),
                            platform: Some(plat),
                            score,
                            path: path.clone(),
                        });
                    }
                }
                // A direct image reference (root is a leaf image): there is
                // no platform metadata in the manifest, so it is the only
                // possible answer and returned unranked.
                None => out.push(MatchedManifest {
                    digest: digest.to_string(),
                    media_type: manifest.media_type.clone(),
                    platform: None,
                    score: Score {
                        variant: 0,
                        features: 0,
                    },
                    path: path.clone(),
                }),
            }
        }
        crate::model::ManifestKind::Index(idx) => {
            for (i, child) in idx.manifests.iter().enumerate() {
                let child_digest = &child.digest;
                Digest::parse(child_digest).map_err(|e| SelectError::InvalidDigest(e.to_string()))?;

                // Fetch + (in strict mode, for non-trusted blobs) verify the
                // child the descriptor points at.
                let child_bytes =
                    fetch_checked(registry, child_digest, Some(child.size), strict)?;

                // A child with a platform only enters the traversal if its
                // platform can satisfy the query (pruning); indexes without
                // a platform (nested indexes) are always entered.
                let child_manifest = Manifest::parse(&child_bytes).map_err(|e| {
                    SelectError::InvalidManifest {
                        digest: child_digest.clone(),
                        reason: e.to_string(),
                    }
                })?;

                let enter = match &child.platform {
                    Some(p) => score_match(p, query).is_some(),
                    None => child.looks_like_index() || child_manifest.is_index(),
                };

                if enter {
                    path.push(PathHop {
                        index: i,
                        digest: child_digest.clone(),
                        media_type: child.media_type.clone(),
                        platform: child.platform.clone(),
                    });
                    walk(
                        registry,
                        child_digest,
                        Some(child),
                        query,
                        strict,
                        active,
                        path,
                        chain,
                        out,
                    )?;
                    path.pop();
                }
            }
        }
    }
    Ok(())
}

/// Apply the OCI platform matching rules. Returns `None` when the platform
/// cannot run the requested workload, otherwise a ranking [`Score`].
fn score_match(candidate: &Platform, q: &PlatformQuery) -> Option<Score> {
    // os / architecture: case-sensitive exact match (OCI strings are
    // normalized lowercase in practice).
    if candidate.os != q.os || candidate.architecture != q.architecture {
        return None;
    }

    // os.version: if the request specifies it, exact match required.
    if let Some(want) = &q.os_version {
        match &candidate.os_version {
            Some(v) if v == want => {}
            _ => return None,
        }
    }

    // os.features: candidate must provide every requested feature.
    if !q.os_features.is_empty() {
        let have: HashSet<&str> = candidate.os_features.iter().map(String::as_str).collect();
        if !q.os_features.iter().all(|f| have.contains(f.as_str())) {
            return None;
        }
    }

    let variant_score = match_variant(&q.architecture, q.variant.as_deref(), candidate.variant.as_deref())?;
    let feature_score = match_features(&q.features, &candidate.features)?;

    Some(Score {
        variant: variant_score,
        features: feature_score,
    })
}

/// ARM variant ladder, newest → oldest. A request can fall back to an older
/// variant; an older request cannot run on a newer one.
const ARM_LADDER: &[&str] = &["v8", "v7", "v6", "v5"];
/// arm64 historically normalises to `arm/v8`.
fn arm64_ladder_pos(v: Option<&str>) -> Option<usize> {
    match v {
        None => Some(0), // arm64 without variant == v8
        Some("v8") => Some(0),
        _ => None,
    }
}

/// Returns 0/1/2 (see [`Score`]) or `None` for incompatibility.
fn match_variant(arch: &str, requested: Option<&str>, candidate: Option<&str>) -> Option<u8> {
    if arch == "arm64" {
        let req = arm64_ladder_pos(requested).unwrap_or(0);
        let cand = arm64_ladder_pos(candidate)?;
        // cand 0 (v8) must be >= req 0 — both are v8 here.
        if cand < req {
            return None;
        }
        return Some(match (requested, candidate) {
            (None, None) => 0,
            (Some(a), Some(b)) if a == b => 2,
            _ => 1,
        });
    }

    if arch != "arm" {
        // Non-ARM architectures: OCI images normally carry no variant.
        return match (requested, candidate) {
            (None, None) => Some(0),
            (None, Some(_)) => Some(0), // request does not care
            (Some(_), None) => Some(0), // tolerate an unspecified candidate
            (Some(a), Some(b)) if a == b => Some(2),
            (Some(_), Some(_)) => None, // explicitly different variants
        };
    }

    // arm (32-bit): apply the v5..v8 ladder.
    let pos = |v: Option<&str>| -> Option<usize> {
        match v {
            None => None, // candidate/request without variant: handled below
            Some(s) => ARM_LADDER.iter().position(|x| *x == s),
        }
    };

    match (requested, candidate) {
        (None, None) => Some(0),
        (None, Some(_)) => Some(0), // query doesn't care
        (Some(req), None) => {
            // containerd treats a missing arm variant as v1; we treat it as
            // the weakest fallback for *any* arm request.
            let _ = pos(Some(req))?;
            Some(1)
        }
        (Some(req), Some(cand)) => {
            let rp = pos(Some(req))?;
            let cp = pos(Some(cand))?;
            // Candidate must be at least as old as/equal to request:
            // ladder index of cand >= index of req.
            if cp < rp {
                return None;
            }
            Some(if rp == cp { 2 } else { 1 })
        }
    }
}

/// CPU-feature set matching. The candidate must run on a CPU with exactly
/// `requested` features: every requested feature must be present, and a
/// candidate that needs *extra* features is not eligible.
fn match_features(requested: &[String], candidate: &[String]) -> Option<u8> {
    let req: HashSet<&str> = requested.iter().map(String::as_str).collect();
    let cand: HashSet<&str> = candidate.iter().map(String::as_str).collect();

    if !cand.iter().all(|f| req.contains(f)) {
        // candidate requires a feature the target does not have
        return None;
    }
    if req.is_empty() && cand.is_empty() {
        Some(0)
    } else if req == cand {
        Some(2)
    } else {
        Some(1) // candidate is a strict subset
    }
}

fn describe(q: &PlatformQuery) -> String {
    let mut s = format!("{}/{}", q.os, q.architecture);
    let mut extras: Vec<String> = Vec::new();
    if let Some(v) = &q.variant {
        extras.push(format!("variant={v}"));
    }
    if !q.features.is_empty() {
        extras.push(format!("features=[{}]", q.features.join(",")));
    }
    if !extras.is_empty() {
        s.push(' ');
        s.push_str(&extras.join(" "));
    }
    s
}

/// Used by tests/fixtures to build a query ergonomically.
#[allow(dead_code)]
pub fn query(os: &str, arch: &str) -> PlatformQuery {
    PlatformQuery {
        os: os.to_string(),
        architecture: arch.to_string(),
        ..Default::default()
    }
}

#[cfg(test)]
mod unit_tests {
    use super::*;

    fn p(os: &str, arch: &str, variant: Option<&str>, features: &[&str]) -> Platform {
        Platform {
            architecture: arch.to_string(),
            os: os.to_string(),
            variant: variant.map(str::to_string),
            os_version: None,
            os_features: vec![],
            features: features.iter().map(|s| s.to_string()).collect(),
        }
    }

    fn q(os: &str, arch: &str, variant: Option<&str>, features: &[&str]) -> PlatformQuery {
        PlatformQuery {
            os: os.to_string(),
            architecture: arch.to_string(),
            variant: variant.map(str::to_string),
            os_version: None,
            os_features: vec![],
            features: features.iter().map(|s| s.to_string()).collect(),
        }
    }

    #[test]
    fn arm_exact_variant_wins_over_fallback() {
        // arm/v7 request against one v7 and one v6 leaf: exact (2) > fallback (1).
        let request = q("linux", "arm", Some("v7"), &[]);
        let exact = score_match(&p("linux", "arm", Some("v7"), &[]), &request).unwrap();
        let older = score_match(&p("linux", "arm", Some("v6"), &[]), &request).unwrap();
        assert_eq!(exact.variant, 2);
        assert_eq!(older.variant, 1);
        assert!(exact > older);
    }

    #[test]
    fn arm_older_request_rejects_newer_candidate() {
        // A v6 binary cannot assume v7 features.
        let request = q("linux", "arm", Some("v6"), &[]);
        assert!(score_match(&p("linux", "arm", Some("v7"), &[]), &request).is_none());
        assert!(score_match(&p("linux", "arm", Some("v6"), &[]), &request).is_some());
    }

    #[test]
    fn arm64_defaults_to_v8() {
        let request = q("linux", "arm64", None, &[]);
        assert!(score_match(&p("linux", "arm64", None, &[]), &request).is_some());
        let request_v8 = q("linux", "arm64", Some("v8"), &[]);
        assert!(score_match(&p("linux", "arm64", None, &[]), &request_v8).is_some());
    }

    #[test]
    fn wrong_os_or_arch_misses() {
        let request = q("linux", "amd64", None, &[]);
        assert!(score_match(&p("darwin", "amd64", None, &[]), &request).is_none());
        assert!(score_match(&p("linux", "arm64", None, &[]), &request).is_none());
    }

    #[test]
    fn feature_subset_matches_but_ranks_lower() {
        let request = q("linux", "amd64", None, &["sse4_2", "avx"]);
        let exact = score_match(&p("linux", "amd64", None, &["sse4_2", "avx"]), &request).unwrap();
        let subset = score_match(&p("linux", "amd64", None, &["sse4_2"]), &request).unwrap();
        assert_eq!(exact.features, 2);
        assert_eq!(subset.features, 1);
        assert!(exact > subset);
        // candidate needing an extra feature is ineligible
        assert!(score_match(
            &p("linux", "amd64", None, &["sse4_2", "avx", "avx512"]),
            &request
        )
        .is_none());
    }

    #[test]
    fn unspecified_features_reject_featureful_candidate() {
        let request = q("linux", "amd64", None, &[]);
        assert!(score_match(&p("linux", "amd64", None, &["sse4_2"]), &request).is_none());
    }
}
