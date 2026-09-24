//! HTTP interface: request validation, planning, applying and queries.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use base64::Engine;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::apply;
use crate::model::{Conflict, ConflictKind, EntryKind, OutputSpec, Plan};
use crate::path::{normalize_target, RelPath};
use crate::planner::{build_plan, sha256_hex};

const MAX_BODY: usize = 16 * 1024 * 1024;

// ----------------------------- state --------------------------------------

#[derive(Clone)]
pub struct AppState {
    inner: Arc<Inner>,
}

struct Inner {
    base_dir: PathBuf,
    records: Mutex<BTreeMap<String, MergeRecord>>,
}

#[derive(Debug, Clone)]
struct MergeRecord {
    merge_id: String,
    root: PathBuf,
    case_sensitive: bool,
    declared_outputs: usize,
    created_at_unix: u64,
}

pub fn app(base_dir: PathBuf) -> Router {
    let state = AppState {
        inner: Arc::new(Inner {
            base_dir,
            records: Mutex::new(BTreeMap::new()),
        }),
    };
    Router::new()
        .route("/", get(index))
        .route("/health", get(health))
        .route("/merge", post(merge))
        .route("/merge/{merge_id}", get(get_merge))
        .with_state(state)
}

// ----------------------------- DTOs ---------------------------------------

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct MergeRequest {
    merge_id: Option<String>,
    case_sensitive: Option<bool>,
    dry_run: Option<bool>,
    actions: Vec<ActionInput>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ActionInput {
    id: String,
    outputs: Vec<OutputInput>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct OutputInput {
    path: String,
    kind: String,
    content: Option<String>,
    content_base64: Option<String>,
    link_target: Option<String>,
}

// ----------------------------- helpers ------------------------------------

fn err_json(status: StatusCode, message: impl Into<String>, extra: Value) -> Response {
    let mut v = json!({ "error": message.into() });
    if let (Some(root), Some(obj)) = (v.as_object_mut(), extra.as_object()) {
        for (k, val) in obj {
            root.insert(k.clone(), val.clone());
        }
    }
    (status, Json(v)).into_response()
}

fn bad_request(message: impl Into<String>) -> Response {
    err_json(StatusCode::BAD_REQUEST, message, json!({}))
}

fn plan_json(plan: &Plan) -> Value {
    json!({
        "nodes": plan.nodes.iter().map(|n| {
            let mut v = json!({
                "path": n.path.as_str(),
                "kind": n.kind.as_str(),
                "actions": n.actions.iter().map(|p| json!({"action": p.action, "index": p.index})).collect::<Vec<_>>(),
            });
            if let Some(h) = &n.content_hash {
                v.as_object_mut().unwrap().insert("content_sha256".into(), json!(h));
            }
            if let Some(t) = &n.link_target {
                v.as_object_mut().unwrap().insert("link_target".into(), json!(t));
            }
            v
        }).collect::<Vec<_>>(),
        "shared_content": plan.shared.iter().map(|s| json!({
            "content_sha256": s.content_hash,
            "paths": s.paths,
            "actions": s.actions,
            "byte_len": s.byte_len,
        })).collect::<Vec<_>>(),
    })
}

fn conflicts_json(conflicts: &[Conflict]) -> Value {
    json!(conflicts
        .iter()
        .map(|c| json!({
            "kind": label(c.kind),
            "path": c.path,
            "other_paths": c.other_paths,
            "actions": c.actions,
            "detail": c.detail,
        }))
        .collect::<Vec<_>>())
}

fn label(k: ConflictKind) -> &'static str {
    k.as_str()
}

fn validate_merge_id(id: &str) -> bool {
    if id.is_empty() || id.len() > 128 {
        return false;
    }
    // No leading '.', no '/', so it can never collide with staging temp dirs.
    if id == "." || id == ".." || id.starts_with('.') || id.contains('/') {
        return false;
    }
    id.chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

/// Action id -> ordered outputs.
type Actions = BTreeMap<String, Vec<OutputSpec>>;
/// Validated request: planner inputs plus total declared output count.
type Validated = Result<(Actions, usize), (StatusCode, String)>;

/// Validate the request and convert it into planner inputs.
fn validate(req: MergeRequest) -> Validated {
    if req.actions.is_empty() {
        return Err((
            StatusCode::BAD_REQUEST,
            "actions must contain at least one action".into(),
        ));
    }
    let mut actions: BTreeMap<String, Vec<OutputSpec>> = BTreeMap::new();
    let mut declared_outputs = 0usize;

    for action in &req.actions {
        if action.id.is_empty() {
            return Err((StatusCode::BAD_REQUEST, "an action has an empty id".into()));
        }
        if actions.contains_key(&action.id) {
            return Err((
                StatusCode::BAD_REQUEST,
                format!("duplicate action id: {}", action.id),
            ));
        }
        if action.outputs.is_empty() {
            return Err((
                StatusCode::BAD_REQUEST,
                format!("action {} declares no outputs", action.id),
            ));
        }
        let mut specs = Vec::with_capacity(action.outputs.len());
        for (i, out) in action.outputs.iter().enumerate() {
            let at = format!("action {} output #{} ({})", action.id, i, out.path);
            let rel = RelPath::parse(&out.path).map_err(|e| {
                (
                    StatusCode::BAD_REQUEST,
                    format!("invalid path for {at}: {e}"),
                )
            })?;
            let kind = match out.kind.as_str() {
                "file" => EntryKind::File,
                "dir" => EntryKind::Dir,
                "symlink" => EntryKind::Symlink,
                other => {
                    return Err((
                        StatusCode::BAD_REQUEST,
                        format!("invalid kind {other:?} for {at}; expected file|dir|symlink"),
                    ))
                }
            };

            let spec = match kind {
                EntryKind::File => {
                    let bytes = match (&out.content, &out.content_base64) {
                        (Some(_), Some(_)) => return Err((
                            StatusCode::BAD_REQUEST,
                            format!(
                                "file {at} sets both content and content_base64; set exactly one"
                            ),
                        )),
                        (Some(s), None) => s.as_bytes().to_vec(),
                        (None, Some(b64)) => base64::engine::general_purpose::STANDARD
                            .decode(b64)
                            .map_err(|e| {
                                (
                                    StatusCode::BAD_REQUEST,
                                    format!("invalid content_base64 for {at}: {e}"),
                                )
                            })?,
                        (None, None) => {
                            return Err((
                                StatusCode::BAD_REQUEST,
                                format!("file {at} requires content or content_base64"),
                            ))
                        }
                    };
                    let hash = sha256_hex(&bytes);
                    OutputSpec {
                        path: rel,
                        kind,
                        content: Some(bytes),
                        content_hash: Some(hash),
                        link_target: None,
                    }
                }
                EntryKind::Dir => {
                    if out.content.is_some() || out.content_base64.is_some() {
                        return Err((
                            StatusCode::BAD_REQUEST,
                            format!("dir {at} must not carry file content"),
                        ));
                    }
                    if out.link_target.is_some() {
                        return Err((
                            StatusCode::BAD_REQUEST,
                            format!("dir {at} must not carry link_target"),
                        ));
                    }
                    OutputSpec {
                        path: rel,
                        kind,
                        content: None,
                        content_hash: None,
                        link_target: None,
                    }
                }
                EntryKind::Symlink => {
                    let target = out.link_target.as_ref().ok_or_else(|| {
                        (
                            StatusCode::BAD_REQUEST,
                            format!("symlink {at} requires link_target"),
                        )
                    })?;
                    // Only NUL/empty make a target malformed at request level;
                    // escaping the root is a planned conflict (HTTP 409).
                    normalize_target(target, &rel).map_err(|e| {
                        (
                            StatusCode::BAD_REQUEST,
                            format!("invalid link_target for {at}: {e}"),
                        )
                    })?;
                    if out.content.is_some() || out.content_base64.is_some() {
                        return Err((
                            StatusCode::BAD_REQUEST,
                            format!("symlink {at} must not carry file content"),
                        ));
                    }
                    OutputSpec {
                        path: rel,
                        kind,
                        content: None,
                        content_hash: None,
                        link_target: Some(target.clone()),
                    }
                }
            };
            specs.push(spec);
            declared_outputs += 1;
        }
        actions.insert(action.id.clone(), specs);
    }
    Ok((actions, declared_outputs))
}

#[derive(Clone, Copy)]
struct Counts {
    files: usize,
    dirs: usize,
    symlinks: usize,
}

fn count_kinds(plan: &Plan) -> Counts {
    let mut c = Counts {
        files: 0,
        dirs: 0,
        symlinks: 0,
    };
    for n in &plan.nodes {
        match n.kind {
            EntryKind::File => c.files += 1,
            EntryKind::Dir => c.dirs += 1,
            EntryKind::Symlink => c.symlinks += 1,
        }
    }
    c
}

fn summary(actions: usize, outputs: usize, c: Counts, shared: usize) -> Value {
    json!({
        "actions": actions,
        "declared_outputs": outputs,
        "files": c.files,
        "dirs": c.dirs,
        "symlinks": c.symlinks,
        "shared_content_groups": shared,
    })
}

fn generate_merge_id(st: &AppState) -> String {
    for _ in 0..8 {
        let id = format!("m-{}", uuid::Uuid::new_v4().simple());
        if st
            .inner
            .records
            .lock()
            .expect("records lock")
            .contains_key(&id)
        {
            continue;
        }
        if st.inner.base_dir.join(&id).exists() {
            continue;
        }
        return id;
    }
    format!("m-{}", uuid::Uuid::new_v4().simple())
}

// ----------------------------- handlers -----------------------------------

async fn index() -> Json<Value> {
    Json(json!({
        "service": "build-output-merge",
        "endpoints": {
            "GET /health": "liveness",
            "POST /merge": "plan and (unless dry_run) atomically apply a merge",
            "GET /merge/{merge_id}": "fetch a previously applied merge record",
        }
    }))
}

async fn health(State(st): State<AppState>) -> Json<Value> {
    Json(json!({
        "status": "ok",
        "base_dir": st.inner.base_dir.to_string_lossy(),
    }))
}

#[allow(clippy::needless_pass_by_value)]
async fn merge(State(st): State<AppState>, body: Body) -> Response {
    let bytes = match axum::body::to_bytes(body, MAX_BODY).await {
        Ok(b) => b,
        Err(e) => return bad_request(format!("failed to read request body: {e}")),
    };
    let req: MergeRequest = match serde_json::from_slice(&bytes) {
        Ok(r) => r,
        Err(e) => return bad_request(format!("invalid JSON request: {e}")),
    };

    let case_sensitive = req.case_sensitive.unwrap_or(false);
    let dry_run = req.dry_run.unwrap_or(false);
    let requested_id = req.merge_id.clone();

    if let Some(id) = &requested_id {
        if !validate_merge_id(id) {
            return bad_request(format!(
                "invalid merge_id {id:?}: 1-128 chars, [A-Za-z0-9_-], must not start with '.'"
            ));
        }
    }

    let (actions, declared_outputs) = match validate(req) {
        Ok(v) => v,
        Err((status, msg)) => return err_json(status, msg, json!({})),
    };

    let plan = build_plan(&actions, case_sensitive);

    let plan_val = plan_json(&plan);
    let conflicts_val = conflicts_json(&plan.conflicts);
    let counts = count_kinds(&plan);
    let action_count = plan.action_ids.len();
    let shared_count = plan.shared.len();

    // Conflicts -> 409, nothing is written.
    if !plan.conflicts.is_empty() {
        return err_json(
            StatusCode::CONFLICT,
            "merge plan has conflicts; target tree was not modified",
            json!({
                "status": "conflicts",
                "conflict_count": plan.conflicts.len(),
                "conflicts": conflicts_val,
                "summary": summary(action_count, declared_outputs, counts, shared_count),
                "plan": plan_val,
            }),
        );
    }

    if dry_run {
        return (
            StatusCode::OK,
            Json(json!({
                "status": "planned",
                "dry_run": true,
                "case_sensitive": case_sensitive,
                "summary": summary(action_count, declared_outputs, counts, shared_count),
                "plan": plan_val,
            })),
        )
            .into_response();
    }

    // Resolve the merge id and reserve it before touching the filesystem.
    let merge_id = match &requested_id {
        Some(id) => {
            let mut records = st.inner.records.lock().expect("records lock");
            if records.contains_key(id) || st.inner.base_dir.join(id).exists() {
                return err_json(
                    StatusCode::CONFLICT,
                    format!("merge_id {id:?} is already in use"),
                    json!({ "kind": "merge_id_taken" }),
                );
            }
            // Reserve the slot so concurrent requests with the same id cannot
            // both pass the check (single-process mutex).
            records.insert(
                id.clone(),
                MergeRecord {
                    merge_id: id.clone(),
                    root: PathBuf::new(),
                    case_sensitive,
                    declared_outputs,
                    created_at_unix: 0,
                },
            );
            id.clone()
        }
        None => generate_merge_id(&st),
    };

    let base_dir = st.inner.base_dir.clone();
    let id_for_apply = merge_id.clone();
    let plan_for_apply = plan.clone();
    let applied = tokio::task::spawn_blocking(move || {
        apply::apply_plan(&base_dir, &id_for_apply, &plan_for_apply)
    })
    .await;

    let root = match applied {
        Ok(Ok(p)) => p,
        Ok(Err(e)) => {
            // Roll back the reservation on failure.
            st.inner
                .records
                .lock()
                .expect("records lock")
                .remove(&merge_id);
            if e.kind() == std::io::ErrorKind::AlreadyExists {
                return err_json(
                    StatusCode::CONFLICT,
                    format!("merge target already exists: {e}"),
                    json!({ "kind": "target_exists" }),
                );
            }
            return err_json(
                StatusCode::INTERNAL_SERVER_ERROR,
                format!("applying merge failed: {e}"),
                json!({}),
            );
        }
        Err(e) => {
            st.inner
                .records
                .lock()
                .expect("records lock")
                .remove(&merge_id);
            return err_json(
                StatusCode::INTERNAL_SERVER_ERROR,
                format!("apply task panicked: {e}"),
                json!({}),
            );
        }
    };

    let record = MergeRecord {
        merge_id: merge_id.clone(),
        root: root.clone(),
        case_sensitive,
        declared_outputs,
        created_at_unix: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0),
    };
    st.inner
        .records
        .lock()
        .expect("records lock")
        .insert(merge_id.clone(), record);

    (
        StatusCode::OK,
        Json(json!({
            "status": "applied",
            "merge_id": merge_id,
            "case_sensitive": case_sensitive,
            "summary": summary(action_count, declared_outputs, counts, shared_count),
            "plan": plan_val,
            "root": root.to_string_lossy(),
        })),
    )
        .into_response()
}

async fn get_merge(State(st): State<AppState>, Path(merge_id): Path<String>) -> Response {
    let records = st.inner.records.lock().expect("records lock");
    match records.get(&merge_id) {
        None => err_json(
            StatusCode::NOT_FOUND,
            format!("no applied merge with id {merge_id:?}"),
            json!({}),
        ),
        Some(r) => (
            StatusCode::OK,
            Json(json!({
                "status": "applied",
                "merge_id": r.merge_id,
                "root": r.root.to_string_lossy(),
                "case_sensitive": r.case_sensitive,
                "declared_outputs": r.declared_outputs,
                "created_at_unix": r.created_at_unix,
            })),
        )
            .into_response(),
    }
}
