//! Axum HTTP service: routes, handlers, error envelope.

use axum::{
    body::{Bytes, HttpBody},
    extract::{Path, Query, State},
    http::{
        header,
        HeaderName,
        HeaderMap,
        StatusCode,
    },
    response::{IntoResponse, Response},
    routing::get,
    Json, Router,
};
use serde::{Deserialize, Serialize};
use std::sync::Arc;

use crate::digest::Digest;
use crate::model::Manifest;
use crate::selector::{self, PlatformQuery, SelectError};
use crate::store::Registry;

/// Shared application state.
#[derive(Clone)]
pub struct AppState {
    pub registry: Arc<Registry>,
}

impl AppState {
    pub fn new(registry: Arc<Registry>) -> Self {
        Self { registry }
    }
}

/// Build the router (also used by in-process integration tests).
///
/// Repository names may contain slashes (`demo/app`), which axum's normal
/// single-segment params cannot capture. Everything under `/v2/` is
/// therefore matched with a wildcard and dispatched by suffix, exactly like
/// a real OCI distribution endpoint would.
pub fn build_app(registry: Arc<Registry>) -> Router {
    let state = AppState::new(registry);
    Router::new()
        .route("/", get(|| async { "OCI manifest selector service" }))
        .route("/healthz", get(|| async { Json(serde_json::json!({"ok": true})) }))
        .route("/v2/", get(|| async { Json(serde_json::json!({"version": 2})) }))
        .route(
            "/v2/{*rest}",
            get(dispatch_get)
                .put(dispatch_put)
                .post(dispatch_post),
        )
        .route("/admin/verify", get(admin_verify))
        .with_state(state)
        .fallback(fallback)
}

/// Parsed shape of a `/v2/...` path.
#[derive(Debug)]
enum ApiPath {
    TagsList { repo: String },
    Blob { repo: String, digest: String },
    BlobUpload { repo: String, _uuid: Option<String> },
    Manifest { repo: String, reference: String },
    Select { repo: String },
}

/// Parse a wildcard path body into an [`ApiPath`].
fn parse_api_path(raw: &str) -> Option<ApiPath> {
    let rest = raw.trim_start_matches('/').trim_end_matches('/');
    if rest.is_empty() {
        return None;
    }
    let segments: Vec<&str> = rest.split('/').collect();

    // `<repo>/tags/list`
    if segments.len() >= 3 && segments[segments.len() - 2..] == ["tags", "list"] {
        return Some(ApiPath::TagsList {
            repo: segments[..segments.len() - 2].join("/"),
        });
    }
    // `<repo>/select`
    if segments.last() == Some(&"select") {
        return Some(ApiPath::Select {
            repo: segments[..segments.len() - 1].join("/"),
        });
    }

    // Locate a keyword segment (`blobs` / `manifests`).
    for (i, seg) in segments.iter().enumerate() {
        match *seg {
            "blobs" => {
                let repo = segments[..i].join("/");
                let tail = &segments[i + 1..];
                return match tail {
                    ["uploads"] => Some(ApiPath::BlobUpload { repo, _uuid: None }),
                    ["uploads", uuid] => Some(ApiPath::BlobUpload {
                        repo,
                        _uuid: Some((*uuid).to_string()),
                    }),
                    [digest] if !digest.is_empty() => Some(ApiPath::Blob {
                        repo,
                        digest: (*digest).to_string(),
                    }),
                    _ => None,
                };
            }
            "manifests" => {
                let repo = segments[..i].join("/");
                let tail = &segments[i + 1..];
                return match tail {
                    [reference] if !reference.is_empty() => Some(ApiPath::Manifest {
                        repo,
                        reference: (*reference).to_string(),
                    }),
                    _ => None,
                };
            }
            _ => {}
        }
    }
    None
}

async fn dispatch_get(
    State(st): State<AppState>,
    Path(rest): Path<String>,
    Query(params): Query<serde_json::Map<String, serde_json::Value>>,
) -> Result<Response, ApiError> {
    let _ = params;
    match parse_api_path(&rest) {
        Some(ApiPath::TagsList { repo }) => list_tags(State(st), Path(repo)).await.map(IntoResponse::into_response),
        Some(ApiPath::Blob { repo, digest }) => {
            get_blob(State(st), Path((repo, digest))).await
        }
        Some(ApiPath::Manifest { repo, reference }) => {
            get_manifest(State(st), Path((repo, reference))).await
        }
        _ => Err(ApiError::new(
            StatusCode::NOT_FOUND,
            "NOT_FOUND",
            "no such route under /v2/",
        )),
    }
}

async fn dispatch_put(
    State(st): State<AppState>,
    Path(rest): Path<String>,
    Query(q): Query<PutQuery>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Response, ApiError> {
    match parse_api_path(&rest) {
        Some(ApiPath::BlobUpload { repo, _uuid: _ }) => {
            let digest = q.digest.ok_or_else(|| {
                ApiError::new(StatusCode::BAD_REQUEST, "BAD_REQUEST", "missing ?digest=")
            })?;
            store_blob(&st, &repo, &digest, body)
        }
        Some(ApiPath::Manifest { repo, reference }) => {
            put_manifest(
                State(st),
                Path((repo, reference)),
                Query(ManifestQuery { strict: q.strict }),
                headers,
                body,
            )
            .await
        }
        _ => Err(ApiError::new(
            StatusCode::NOT_FOUND,
            "NOT_FOUND",
            "no such route under /v2/",
        )),
    }
}

async fn dispatch_post(
    State(st): State<AppState>,
    Path(rest): Path<String>,
    Query(params): Query<SelectParams>,
    Json(body): Json<SelectBody>,
) -> Result<Response, ApiError> {
    match parse_api_path(&rest) {
        Some(ApiPath::Select { repo }) => {
            select_target(State(st), Path(repo), Query(params), Json(body))
                .await
                .map(IntoResponse::into_response)
        }
        _ => Err(ApiError::new(
            StatusCode::NOT_FOUND,
            "NOT_FOUND",
            "no such route under /v2/",
        )),
    }
}

// ---------- error envelope ------------------------------------------------

#[derive(Debug, Serialize)]
struct ErrorBody {
    errors: [ErrorDetail; 1],
}

#[derive(Debug, Serialize)]
struct ErrorDetail {
    code: String,
    message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    detail: Option<serde_json::Value>,
}

struct ApiError {
    status: StatusCode,
    code: String,
    message: String,
    detail: Option<serde_json::Value>,
}

impl ApiError {
    fn new(status: StatusCode, code: &str, message: impl Into<String>) -> Self {
        Self {
            status,
            code: code.to_string(),
            message: message.into(),
            detail: None,
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let body = ErrorBody {
            errors: [ErrorDetail {
                code: self.code,
                message: self.message,
                detail: self.detail,
            }],
        };
        (self.status, Json(body)).into_response()
    }
}

fn select_error_status(e: &SelectError) -> StatusCode {
    match e {
        SelectError::ReferenceNotFound(_) => StatusCode::NOT_FOUND,
        SelectError::BlobMissing(_) => StatusCode::NOT_FOUND,
        SelectError::InvalidDigest(_) | SelectError::BadRequest(_) => StatusCode::BAD_REQUEST,
        SelectError::InvalidManifest { .. } => StatusCode::UNPROCESSABLE_ENTITY,
        SelectError::Verification { .. } => StatusCode::UNPROCESSABLE_ENTITY,
        SelectError::Cycle(_) => StatusCode::UNPROCESSABLE_ENTITY,
        SelectError::NoMatch { .. } => StatusCode::NOT_FOUND,
        SelectError::Ambiguous { .. } => StatusCode::CONFLICT,
    }
}

async fn fallback() -> ApiError {
    ApiError::new(StatusCode::NOT_FOUND, "NOT_FOUND", "no such route")
}

// ---------- handlers ------------------------------------------------------

async fn list_tags(
    State(st): State<AppState>,
    Path(repo): Path<String>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let tags = st.registry.list_tags(&repo);
    Ok(Json(serde_json::json!({ "name": repo, "tags": tags })))
}

async fn get_blob(
    State(st): State<AppState>,
    Path((_repo, digest)): Path<(String, String)>,
) -> Result<Response, ApiError> {
    Digest::parse(&digest).map_err(|e| ApiError::new(StatusCode::BAD_REQUEST, "INVALID_DIGEST", e.to_string()))?;
    let data = st
        .registry
        .get_blob(&digest)
        .ok_or_else(|| ApiError::new(StatusCode::NOT_FOUND, "BLOB_UNKNOWN", format!("blob {digest} not found")))?;
    Ok((
        StatusCode::OK,
        [(header::CONTENT_TYPE, "application/octet-stream")],
        data,
    )
        .into_response())
}

#[derive(Debug, Deserialize)]
struct PutQuery {
    /// Blob upload: content digest the body must hash to.
    #[serde(default)]
    digest: Option<String>,
    /// Manifest PUT by digest (default true): enforce content hash.
    #[serde(default = "default_true")]
    strict: Option<bool>,
}
fn default_true() -> Option<bool> {
    Some(true)
}

fn store_blob(
    st: &AppState,
    repo: &str,
    digest: &str,
    body: Bytes,
) -> Result<Response, ApiError> {
    Digest::parse(digest)
        .map_err(|e| ApiError::new(StatusCode::BAD_REQUEST, "INVALID_DIGEST", e.to_string()))?;
    st.registry
        .put_verified(digest, body.to_vec())
        .map_err(|e| match e {
            crate::store::StoreError::Digest(d) => {
                ApiError::new(StatusCode::BAD_REQUEST, "DIGEST_MISMATCH", d.to_string())
            }
            other => ApiError::new(StatusCode::CONFLICT, "BLOB_CONFLICT", other.to_string()),
        })?;
    let location = format!("/v2/{repo}/blobs/{digest}");
    Ok((
        StatusCode::CREATED,
        [(header::LOCATION, location)],
        Json(serde_json::json!({ "digest": digest })),
    )
        .into_response())
}

/// PUT query parameters for a manifest: only the strictness toggle.
#[derive(Debug, Deserialize)]
struct ManifestQuery {
    #[serde(default = "default_true")]
    strict: Option<bool>,
}

/// PUT a manifest. Reference may be a tag (`1.0`) or a digest.
async fn put_manifest(
    State(st): State<AppState>,
    Path((repo, reference)): Path<(String, String)>,
    Query(q): Query<ManifestQuery>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Response, ApiError> {
    let ctype = headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or(crate::model::MT_IMAGE);

    // Parse first — rejects garbage early with a clear error.
    let manifest = Manifest::parse(&body).map_err(|e| {
        ApiError::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            e.code(),
            e.to_string(),
        )
    })?;

    // Verify every descriptor the manifest points at exists (its digest is
    // recomputed again at selection time, so this is an early convenience).
    for d in manifest.children() {
        Digest::parse(&d.digest).map_err(|e| {
            ApiError::new(StatusCode::BAD_REQUEST, "INVALID_DIGEST", e.to_string())
        })?;
        if !st.registry.has_blob(&d.digest) {
            return Err(ApiError::new(
                StatusCode::NOT_FOUND,
                "BLOB_UNKNOWN",
                format!("referenced blob {} missing; upload it first", d.digest),
            ));
        }
    }
    if let crate::model::ManifestKind::Image(img) = &manifest.kind {
        for d in std::iter::once(&img.config).chain(img.layers.iter()) {
            if !st.registry.has_blob(&d.digest) {
                return Err(ApiError::new(
                    StatusCode::NOT_FOUND,
                    "BLOB_UNKNOWN",
                    format!("referenced blob {} missing; upload it first", d.digest),
                ));
            }
        }
    }

    let computed = Digest::of_bytes(&body);

    // Tag or digest reference?
    let (target_digest, tag_to_set): (String, Option<String>) = if reference.starts_with("sha256:") {
        Digest::parse(&reference).map_err(|e| {
            ApiError::new(StatusCode::BAD_REQUEST, "INVALID_DIGEST", e.to_string())
        })?;
        if q.strict.unwrap_or(true) && reference != computed.as_str() {
            return Err(ApiError::new(
                StatusCode::BAD_REQUEST,
                "DIGEST_MISMATCH",
                format!(
                    "reference digest {reference} does not match content {}",
                    computed
                ),
            ));
        }
        (reference.clone(), None)
    } else {
        (computed.to_string(), Some(reference.clone()))
    };

    st.registry
        .put_verified(&target_digest, body.to_vec())
        .map_err(|e| ApiError::new(StatusCode::CONFLICT, "BLOB_CONFLICT", e.to_string()))?;
    if let Some(tag) = tag_to_set {
        st.registry.tag(&repo, &tag, target_digest.clone());
    }

    let location = format!("/v2/{repo}/manifests/{}", target_digest);
    Ok((
        StatusCode::CREATED,
        [
            (HeaderName::from_static("location"), location),
            (header::CONTENT_TYPE, ctype.to_string()),
            (
                HeaderName::from_static("docker-content-digest"),
                target_digest.clone(),
            ),
        ],
        Json(serde_json::json!({ "digest": target_digest })),
    )
        .into_response())
}

async fn get_manifest(
    State(st): State<AppState>,
    Path((repo, reference)): Path<(String, String)>,
) -> Result<Response, ApiError> {
    let digest = if reference.starts_with("sha256:") {
        Digest::parse(&reference)
            .map_err(|e| ApiError::new(StatusCode::BAD_REQUEST, "INVALID_DIGEST", e.to_string()))?
            .to_string()
    } else {
        st.registry
            .resolve_tag(&repo, &reference)
            .ok_or_else(|| {
                ApiError::new(
                    StatusCode::NOT_FOUND,
                    "MANIFEST_UNKNOWN",
                    format!("{repo}:{reference} not found"),
                )
            })?
    };
    let data = st.registry.get_blob(&digest).ok_or_else(|| {
        ApiError::new(StatusCode::NOT_FOUND, "BLOB_UNKNOWN", format!("blob {digest} missing"))
    })?;
    let parsed = Manifest::parse(&data).map_err(|e| {
        ApiError::new(StatusCode::UNPROCESSABLE_ENTITY, e.code(), e.to_string())
    })?;
    let ctype = parsed
        .media_type
        .unwrap_or_else(|| crate::model::MT_IMAGE.to_string());
    Ok((
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, ctype),
            (
                HeaderName::from_static("docker-content-digest"),
                digest,
            ),
        ],
        data,
    )
        .into_response())
}

#[derive(Debug, Deserialize)]
struct SelectParams {
    #[serde(default = "default_true")]
    strict: Option<bool>,
}

#[derive(Debug, Deserialize)]
struct SelectBody {
    /// `repo:tag` or `repo@sha256:...`
    reference: String,
    #[serde(flatten)]
    platform: PlatformQuery,
}

async fn select_target(
    State(st): State<AppState>,
    Path(repo): Path<String>,
    Query(params): Query<SelectParams>,
    Json(body): Json<SelectBody>,
) -> Result<Json<serde_json::Value>, ApiError> {
    // Allow the reference in the body to be just the tag/digest when the
    // repo is already in the path; also accept fully-qualified references.
    let reference = if body.reference.contains('/') || body.reference.contains('@') {
        body.reference.clone()
    } else {
        // body.reference is a tag or digest; join with path repo
        if body.reference.starts_with("sha256:") {
            format!("{repo}@{}", body.reference)
        } else {
            format!("{}:{}", repo, body.reference)
        }
    };

    let strict = params.strict.unwrap_or(true);
    match selector::select(&st.registry, &reference, &body.platform, strict) {
        Ok(sel) => Ok(Json(serde_json::json!({
            "status": "selected",
            "result": sel,
        }))),
        Err(e) => {
            let status = select_error_status(&e);
            let mut err = ApiError::new(status, e.code(), e.to_string());
            // Ambiguous: attach the tied candidates so the client sees that
            // order-independent tie.
            if let SelectError::Ambiguous { .. } = e {
                err.detail = Some(serde_json::json!({
                    "hint": "multiple manifests satisfy the platform with equal specificity; disambiguate with variant/features or a digest reference"
                }));
            }
            Err(err)
        }
    }
}

async fn admin_verify(State(st): State<AppState>) -> Json<serde_json::Value> {
    Json(serde_json::json!({ "blobs": st.registry.verify_all() }))
}

// `HttpBody` is imported to avoid an unused warning on some toolchains when
// body limits are tuned; keep a tiny use.
#[allow(dead_code)]
fn _body_size_hint<B: HttpBody>(b: &B) -> usize {
    b.size_hint().lower() as usize
}
