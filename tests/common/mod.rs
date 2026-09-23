//! 测试公共设施：在随机端口上真实启动协调器（真 PostgreSQL）与目标桩。
//!
//! 数据库：用 TEST_DATABASE_URL 指定，默认 postgres://localhost/relay_coord_test，
//! 不存在会尝试创建。每个测试使用独立通道；fencing token 全局递增，
//! 因此断言只用"严格递增"而非具体数值，避免测试间耦合。

use relay_coord::client::Client;
use relay_coord::db::Store;
use serde_json::{json, Value};
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;
use tokio::net::TcpListener;

pub struct Harness {
    pub coord_url: String,
    pub target_url: String,
    pub target_state: relay_coord::target::StubState,
    pub store: Arc<Store>,
    _shutdown: tokio::sync::broadcast::Sender<()>,
    /// 持有的跨进程测试锁；drop（连接关闭）时自动释放。
    _lock: sqlx::PgPool,
    /// 进程内测试串行锁的持有许可（drop 时释放）。
    _inproc: tokio::sync::OwnedSemaphorePermit,
}

/// 每个测试二进制内只允许一个 harness 测试同时运行。
static INPROC: std::sync::LazyLock<std::sync::Arc<tokio::sync::Semaphore>> =
    std::sync::LazyLock::new(|| std::sync::Arc::new(tokio::sync::Semaphore::new(1)));

pub fn db_url() -> String {
    std::env::var("TEST_DATABASE_URL")
        .unwrap_or_else(|_| "postgres://admin:relay_coord_dev@localhost/relay_coord_test".to_string())
}

pub async fn ensure_database_pub() {
    ensure_database().await;
}

async fn ensure_database() {
    let url = db_url();
    // 先连库试迁移；失败（库不存在）则连 postgres 库建库。
    if Store::connect(&url).await.is_ok() {
        return;
    }
    let admin = "postgres://admin:relay_coord_dev@localhost/postgres";
    let name = url.rsplit('/').next().unwrap();
    let pool = sqlx::postgres::PgPoolOptions::new()
        .connect(admin)
        .await
        .expect("connect to postgres maintenance db");
    sqlx::query(&format!("CREATE DATABASE {name}"))
        .execute(&pool)
        .await
        .ok();
}

pub async fn spawn_stack() -> Harness {
    let _ = tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "relay_coord=info".into()),
        )
        .with_test_writer()
        .try_init();

    // 在第一个 .await 之前同步抢许可，保证同一二进制内测试串行
    //（避免在 tokio worker 线程内阻塞）。
    let inproc = loop {
        match INPROC.clone().try_acquire_owned() {
            Ok(p) => break p,
            Err(_) => std::thread::sleep(std::time::Duration::from_millis(20)),
        }
    };
    ensure_database().await;
    // 所有测试二进制共享一个库：用跨会话 advisory lock 串行化，
    // 避免一个二进制 enqueue 的消息被另一个二进制的 worker/acquire 看到。
    let lock_pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(1)
        .connect(&db_url())
        .await
        .unwrap();
    sqlx::query("SELECT pg_advisory_lock(932711)")
        .execute(&lock_pool)
        .await
        .unwrap();

    let store = Arc::new(Store::connect(&db_url()).await.unwrap());
    store.migrate().await.unwrap();
    // 测试之间清空业务数据（fencing token 从 1 重新开始，断言更易读）。
    sqlx::raw_sql(
        "TRUNCATE TABLE messages, channels, fencing_seq RESTART IDENTITY CASCADE;
         INSERT INTO fencing_seq (id, value) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;",
    )
    .execute(&store.pool)
    .await
    .unwrap();

    let coord_listener = TcpListener::bind(SocketAddr::from(([127, 0, 0, 1], 0)))
        .await
        .unwrap();
    let coord_port = coord_listener.local_addr().unwrap().port();
    let coord_url = format!("http://127.0.0.1:{coord_port}");

    let target_state = relay_coord::target::StubState::new();
    let target_listener = TcpListener::bind(SocketAddr::from(([127, 0, 0, 1], 0)))
        .await
        .unwrap();
    let target_port = target_listener.local_addr().unwrap().port();
    let target_url = format!("http://127.0.0.1:{target_port}");

    let (tx, mut rx) = tokio::sync::broadcast::channel::<()>(1);

    {
        let store = store.clone();
        let mut rx = tx.subscribe();
        tokio::spawn(async move {
            let app = relay_coord::coordinator::router(store);
            let server = axum::serve(coord_listener, app)
                .with_graceful_shutdown(async move {
                    let _ = rx.recv().await;
                });
            server.await.unwrap();
        });
    }
    {
        let state = target_state.clone();
        let mut rx2 = tx.subscribe();
        tokio::spawn(async move {
            let app = relay_coord::target::shared_router(state);
            let server = axum::serve(target_listener, app)
                .with_graceful_shutdown(async move {
                    let _ = rx2.recv().await;
                });
            server.await.unwrap();
        });
    }

    Harness {
        coord_url,
        target_url,
        target_state,
        store,
        _shutdown: tx,
        _lock: lock_pool,
        _inproc: inproc,
    }
}

impl Harness {
    pub fn client(&self) -> Client {
        Client::new(&self.coord_url, Duration::from_secs(10))
    }

    /// 投递一条消息，返回 submission_id（字符串）。
    pub async fn enqueue(&self, channel: &str, n: i64) -> String {
        let (status, v) = self
            .client()
            .post(
                "/v1/messages",
                json!({"channel_id": channel, "payload": {"seq": n, "kind": "transfer"}}),
            )
            .await
            .unwrap();
        assert_eq!(status, 200, "enqueue failed: {v}");
        v["submission_id"].as_str().unwrap().to_string()
    }

    pub async fn status_of(&self, id: &str) -> Value {
        let (status, v) = self.client().get(&format!("/v1/messages/{id}")).await.unwrap();
        assert_eq!(status, 200);
        v
    }

    /// 起一个进程内 worker（与跨进程 worker 共享同一套 HTTP 协议）。
    pub fn spawn_worker(&self, relay_id: &str, ttl: i64, submit_timeout: u64) {
        let cfg = relay_coord::worker::WorkerConfig {
            coordinator_url: self.coord_url.clone(),
            target_url: self.target_url.clone(),
            relay_id: relay_id.to_string(),
            lease_ttl_secs: ttl,
            submit_timeout: Duration::from_secs(submit_timeout),
            poll_interval: Duration::from_millis(100),
            backoff_base: Duration::from_millis(200),
        };
        let shutdown = Arc::new(tokio::sync::Notify::new());
        tokio::spawn(relay_coord::worker::run_loop(cfg, shutdown));
    }

    /// 真正的独立 OS 进程 worker（`cargo run --bin worker` 的已编译产物）。
    pub fn spawn_worker_process(&self, relay_id: &str, ttl: i64, submit_timeout: u64) -> std::process::Child {
        let bin = env!("CARGO_BIN_EXE_worker");
        std::process::Command::new(bin)
            .args([
                "--coordinator",
                &self.coord_url,
                "--target",
                &self.target_url,
                "--relay-id",
                relay_id,
                "--lease-ttl",
                &ttl.to_string(),
                "--submit-timeout",
                &submit_timeout.to_string(),
                "--poll-ms",
                "100",
            ])
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .expect("spawn worker binary")
    }

    /// 轮询直到消息终态或超时。
    pub async fn wait_status(&self, id: &str, want: &str, timeout: Duration) -> Value {
        let start = std::time::Instant::now();
        loop {
            let v = self.status_of(id).await;
            if v["status"] == want {
                return v;
            }
            assert!(
                start.elapsed() < timeout,
                "message {id} did not reach {want}, last: {v}"
            );
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    }
}
