//! HTTP front-end for the copy-on-write snapshot store.

use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post, put};
use axum::{Json, Router};
use cow_snapstore::store::{Store, StoreError};
use serde::{Deserialize, Serialize};
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

struct AppState {
    store: Store,
}

#[derive(Serialize)]
struct ErrorBody {
    error: String,
}

struct ApiError(StatusCode, String);

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(ErrorBody { error: self.1 })).into_response()
    }
}

impl From<StoreError> for ApiError {
    fn from(e: StoreError) -> Self {
        let code = match &e {
            StoreError::SnapshotNotFound(_) | StoreError::PageNotFound { .. } => {
                StatusCode::NOT_FOUND
            }
            StoreError::SnapshotExists(_) => StatusCode::CONFLICT,
            StoreError::PageTooLarge { .. } | StoreError::InvalidName(_) => {
                StatusCode::BAD_REQUEST
            }
            StoreError::Io(_) | StoreError::Corrupt(_) => StatusCode::INTERNAL_SERVER_ERROR,
        };
        ApiError(code, e.to_string())
    }
}

#[derive(Deserialize)]
struct CreateSnapshotReq {
    name: String,
    /// Optional source snapshot to branch from.
    from: Option<String>,
}

#[derive(Serialize)]
struct CreateSnapshotResp {
    name: String,
    branched_from: Option<String>,
}

async fn create_snapshot(
    State(st): State<Arc<AppState>>,
    Json(req): Json<CreateSnapshotReq>,
) -> Result<(StatusCode, Json<CreateSnapshotResp>), ApiError> {
    st.store.create_snapshot(&req.name, req.from.as_deref())?;
    Ok((
        StatusCode::CREATED,
        Json(CreateSnapshotResp {
            name: req.name,
            branched_from: req.from,
        }),
    ))
}

async fn list_snapshots(State(st): State<Arc<AppState>>) -> Json<Vec<String>> {
    Json(st.store.list_snapshots())
}

async fn snapshot_info(
    State(st): State<Arc<AppState>>,
    Path(name): Path<String>,
) -> Result<Json<cow_snapstore::SnapshotInfo>, ApiError> {
    Ok(Json(st.store.snapshot_info(&name)?))
}

async fn delete_snapshot(
    State(st): State<Arc<AppState>>,
    Path(name): Path<String>,
) -> Result<StatusCode, ApiError> {
    st.store.delete_snapshot(&name)?;
    Ok(StatusCode::NO_CONTENT)
}

async fn write_page(
    State(st): State<Arc<AppState>>,
    Path((name, index)): Path<(String, u64)>,
    body: axum::body::Bytes,
) -> Result<StatusCode, ApiError> {
    st.store.write_page(&name, index, &body)?;
    Ok(StatusCode::NO_CONTENT)
}

async fn read_page(
    State(st): State<Arc<AppState>>,
    Path((name, index)): Path<(String, u64)>,
) -> Result<Vec<u8>, ApiError> {
    Ok(st.store.read_page(&name, index)?)
}

async fn stats(State(st): State<Arc<AppState>>) -> Result<Json<cow_snapstore::Stats>, ApiError> {
    Ok(Json(st.store.stats()?))
}

#[derive(Serialize)]
struct GcResp {
    removed_pages: usize,
}

async fn gc(State(st): State<Arc<AppState>>) -> Result<Json<GcResp>, ApiError> {
    let removed = st.store.gc()?;
    Ok(Json(GcResp {
        removed_pages: removed.len(),
    }))
}

#[tokio::main]
async fn main() {
    let dir: PathBuf = std::env::var("COW_DATA_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("./cow-data"));
    let port: u16 = std::env::var("COW_PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(3000);

    let store = Store::open(&dir).expect("failed to open store");
    eprintln!("cow-snapstore: data dir {}", dir.display());
    eprintln!(
        "cow-snapstore: recovered {} snapshot(s)",
        store.list_snapshots().len()
    );

    let app = Router::new()
        .route("/snapshots", post(create_snapshot).get(list_snapshots))
        .route(
            "/snapshots/{name}",
            get(snapshot_info).delete(delete_snapshot),
        )
        .route(
            "/snapshots/{name}/pages/{index}",
            put(write_page).get(read_page),
        )
        .route("/stats", get(stats))
        .route("/gc", post(gc))
        .with_state(Arc::new(AppState { store }));

    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .expect("bind failed");
    eprintln!("cow-snapstore: listening on http://{addr}");
    axum::serve(listener, app).await.expect("server error");
}
