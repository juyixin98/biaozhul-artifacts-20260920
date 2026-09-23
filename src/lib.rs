//! 配额预留服务核心库（二进制入口见 main.rs）。

pub mod clock;
pub mod error;
pub mod http;
pub mod store;

/// 测试 / 示例装配辅助（临时事件日志、注入时钟、in-process 请求）。
/// 作为独立服务没有下游 crate，直接暴露给集成测试使用。
pub mod support {
    /// 仅供集成测试使用的装配辅助（临时事件日志 + 注入时钟）。
    pub mod test_support {
        use crate::clock::{Clock, InjectedClock, SystemClock};
        use crate::http::{router, AppState};
        use crate::store::Store;
        use axum::body::Body;
        use axum::http::{Request, StatusCode};
        use axum::Router;
        use http_body_util::BodyExt;
        use serde_json::Value;
        use std::path::{Path, PathBuf};
        use std::sync::Arc;
        use tower::ServiceExt;

        /// 带独立临时事件日志的测试应用（drop 时清理临时目录）。
        pub struct TestApp {
            router: Router,
            #[allow(dead_code)]
            dir: tempdir::TempDir,
            data_path: PathBuf,
        }

        impl TestApp {
            pub fn new() -> Self {
                Self::start(1_700_000_000_000)
            }

            fn start(clock_start_ms: i64) -> Self {
                let dir = tempdir::TempDir::new("quota-test").expect("tempdir");
                let data_path = dir.path().join("events.log");
                let clock = InjectedClock::new(clock_start_ms);
                let store = Store::open(&data_path, clock.clone()).expect("open store");
                let router = router(AppState {
                    store,
                    injected: Some(clock),
                    default_ttl_ms: 60_000,
                });
                TestApp {
                    router,
                    dir,
                    data_path,
                }
            }

            /// 复制当前事件日志到一个新的临时目录并用指定时钟时刻重新打开
            /// （模拟"新进程 + 同一持久化文件"重启恢复，且不影响原实例）。
            pub fn reopen_copy(&self, clock_start_ms: i64) -> Self {
                let dir = tempdir::TempDir::new("quota-test-reopen").expect("tempdir");
                let copied = dir.path().join("events.log");
                std::fs::copy(&self.data_path, &copied).expect("copy event log");
                let clock = InjectedClock::new(clock_start_ms);
                let store = Store::open(&copied, clock.clone()).expect("reopen store");
                let router = router(AppState {
                    store,
                    injected: Some(clock),
                    default_ttl_ms: 60_000,
                });
                TestApp {
                    router,
                    dir,
                    data_path: copied,
                }
            }

            pub fn path(&self) -> &Path {
                &self.data_path
            }
        }

        impl std::ops::Deref for TestApp {
            type Target = Router;
            fn deref(&self) -> &Router {
                &self.router
            }
        }

        /// 独立的微型 tempdir（无第三方依赖）。
        mod tempdir {
            pub struct TempDir(std::path::PathBuf);
            impl TempDir {
                pub fn new(prefix: &str) -> std::io::Result<Self> {
                    use std::sync::atomic::{AtomicU64, Ordering};
                    static N: AtomicU64 = AtomicU64::new(0);
                    let n = N.fetch_add(1, Ordering::Relaxed);
                    let p = std::env::temp_dir().join(format!(
                        "{prefix}-{}-{n}",
                        std::process::id()
                    ));
                    std::fs::create_dir_all(&p)?;
                    Ok(TempDir(p))
                }
                pub fn path(&self) -> &std::path::Path {
                    &self.0
                }
            }
            impl Drop for TempDir {
                fn drop(&mut self) {
                    let _ = std::fs::remove_dir_all(&self.0);
                }
            }
        }

        /// 便捷构造：注入时钟、默认 TTL。
        pub fn make_app(quota_bytes: u64, quota_objects: u64) -> Router {
            let _ = (quota_bytes, quota_objects);
            TestApp::new().router.clone()
        }

        /// 系统时钟模式（管理接口推进时钟应失败）。
        pub fn make_system_app() -> Router {
            let dir = tempdir::TempDir::new("quota-test-sys").expect("tempdir");
            let data_path = dir.path().join("events.log");
            let clock: Arc<dyn Clock> = Arc::new(SystemClock);
            let store = Store::open(&data_path, clock).expect("open store");
            // 泄漏临时目录仅用于测试生命周期内（随进程结束被 /tmp 清理策略回收）。
            std::mem::forget(dir);
            router(AppState {
                store,
                injected: None,
                default_ttl_ms: 60_000,
            })
        }

        /// 发送一个 in-process HTTP 请求，返回 (状态码, JSON 体)。
        pub async fn send(
            app: &Router,
            method: &str,
            uri: &str,
            body: Option<Value>,
        ) -> (StatusCode, Value) {
            let mut builder = Request::builder().method(method).uri(uri);
            let body = match body {
                Some(v) => {
                    builder = builder.header("content-type", "application/json");
                    Body::from(v.to_string())
                }
                None => Body::empty(),
            };
            let resp = app
                .clone()
                .oneshot(builder.body(body).unwrap())
                .await
                .expect("response");
            let status = resp.status();
            let bytes = resp
                .into_body()
                .collect()
                .await
                .expect("read body")
                .to_bytes();
            let value = if bytes.is_empty() {
                Value::Null
            } else {
                serde_json::from_slice(&bytes).unwrap_or_else(|e| {
                    panic!("non-JSON body: {e}: {}", String::from_utf8_lossy(&bytes))
                })
            };
            (status, value)
        }

        pub fn read_json(v: Value, _field: &str) -> Value {
            v
        }
    }
}
