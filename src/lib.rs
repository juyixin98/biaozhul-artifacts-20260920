pub mod arbiter;
pub mod clock;
pub mod config;
pub mod crypto;
pub mod db;
pub mod error;
pub mod models;
pub mod server;

/// 集成测试支持：构造真实 AppState / Router、按与客户端相同规则签名。
pub mod test_support {
    pub use crate::crypto::{sign_hex, SigDomain};
    pub use crate::server::{build_clock, build_router, AppState};
    use serde_json::Value;
    use std::sync::Arc;

    pub struct SignedRequest {
        pub body_bytes: Vec<u8>,
        pub signature: String,
    }

    pub fn sign_motion(key_hex: &str, body: &Value) -> SignedRequest {
        sign(key_hex, SigDomain::Motion, body)
    }

    pub fn sign_estop(key_hex: &str, body: &Value) -> SignedRequest {
        sign(key_hex, SigDomain::Estop, body)
    }

    fn sign(key_hex: &str, domain: SigDomain, body: &Value) -> SignedRequest {
        let body_bytes = serde_json::to_vec(body).unwrap();
        let key = hex::decode(key_hex).unwrap();
        let signature = sign_hex(&key, domain, &body_bytes);
        SignedRequest {
            body_bytes,
            signature,
        }
    }

    /// 与示例/验收一致的测试配置：8 路自主 + 2 路额外自主 + 1 遥控 + 1 急停。
    pub fn test_config() -> Value {
        let mut sources = serde_json::Map::new();
        let mut add = |id: &str, kind: &str| {
            // 确定性密钥：由来源 id 填充到 32 字节（仅供测试，生产用 keygen）。
            let mut seed = Vec::new();
            while seed.len() < 32 {
                seed.extend_from_slice(id.as_bytes());
            }
            seed.truncate(32);
            sources.insert(
                id.to_string(),
                serde_json::json!({ "kind": kind, "key_hex": hex::encode(&seed) }),
            );
        };
        for i in 0..8 {
            add(&format!("auto-{i}"), "autonomous");
        }
        add("auto-a", "autonomous");
        add("auto-b", "autonomous");
        add("rc-1", "rc");
        add("estop-1", "estop");
        serde_json::json!({
            "max_future_ms": 5000,
            "max_lease_ms": 5000,
            "limits": { "vx": 1.5, "vy": 1.0, "omega": 2.0 },
            "sources": sources,
        })
    }

    pub fn build_test_state(
        config_path: &str,
        db_path: &str,
        seed_ms: Option<i64>,
    ) -> Arc<AppState> {
        let cfg = crate::config::Config::load(std::path::Path::new(config_path)).unwrap();
        let database = crate::db::Db::open(db_path).unwrap();
        let clock = build_clock("manual", &database, seed_ms);
        Arc::new(AppState {
            cfg,
            db: database,
            clock,
            ingest_lock: std::sync::Mutex::new(()),
        })
    }

    pub fn build_test_router(state: Arc<AppState>) -> axum::Router {
        build_router(state)
    }
}
