//! Library crate so integration tests in `tests/` can drive the real router.

pub mod app;
pub mod error;
pub mod store;

/// Test-only helpers (building a router on a temp directory). Kept out of the
/// binary path; small enough that a cfg gate is unnecessary.
pub mod test_support {
    use std::path::PathBuf;

    use crate::app::{router, AppState};
    use crate::store::Store;

    #[derive(Clone)]
    pub struct AppCfg {
        pub data_dir: PathBuf,
        pub max_body: usize,
    }

    /// Build a fresh router backed by `data_dir` (runs startup reconciliation).
    pub async fn router_on(cfg: AppCfg) -> axum::Router {
        let store = Store::new(cfg.data_dir).await.expect("open store");
        router(AppState {
            store,
            max_body: cfg.max_body,
        })
    }
}
