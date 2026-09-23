//! 端到端测试公共设施：真实 PostgreSQL（relay_coord 库）+ 真实二进制进程
//! （协调器 / 目标桩 / 中继 worker）。无 mock：所有 HTTP、HMAC、租约、事务真实执行。
//!
//! 每个测试启动独立的协调器与桩进程，绑定端口 0（由 OS 分配，杜绝端口竞争），
//! 并从进程 stdout 解析实际监听端口；共享同一个数据库，但每例启动时清空业务表。

#![allow(dead_code)]

use serde_json::{json, Value};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::time::Duration;
use std::io::{BufRead as _, BufReader as StdBufReader};
use std::sync::mpsc;

pub const ADMIN_DB_URL: &str = "postgresql:///postgres";
pub const SECRET: &str = "test-shared-secret";

/// 每个测试用例独立数据库，彻底隔离 channels.next_nonce 等状态。
pub fn per_test_db_url() -> String {
    format!(
        "postgresql:///relay_coord_t{}_{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    )
}

/// 从连接 URL 取数据库名。
fn db_name(url: &str) -> &str {
    url.rsplit('/').next().unwrap()
}

pub fn bin() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push("target/debug/relay-coord");
    p
}

async fn spawn_and_port(args: &[&str], needle: &str) -> (Child, u16) {
    let mut child = Command::new(bin())
        .args(args)
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn service");
    let stdout = child.stdout.take().expect("piped stdout");
    // std 进程的 stdout 只能同步读：起一个阻塞线程扫描监听行，通过 channel 回报端口。
    let (tx, rx) = mpsc::channel::<u16>();
    let needle_owned = needle.to_string();
    std::thread::spawn(move || {
        let reader = StdBufReader::new(stdout);
        for line in reader.lines().map_while(Result::ok) {
            if let Some(p) = parse_port(&line, &needle_owned) {
                let _ = tx.send(p);
                return;
            }
        }
    });
    let mut port = 0u16;
    for _ in 0..100 {
        if let Ok(p) = rx.try_recv() {
            port = p;
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(port > 0, "failed to parse port for {needle}");
    (child, port)
}

fn parse_port(line: &str, needle: &str) -> Option<u16> {
    let idx = line.find(needle)?;
    let rest = &line[idx + needle.len()..];
    // 取 host:port 中最后一个冒号后的端口（rest 形如 127.0.0.1:41809 ...）
    let port_str = rest.rsplit(':').next()?;
    let digits: String = port_str.chars().take_while(|c| c.is_ascii_digit()).collect();
    digits.parse().ok().filter(|&p| p > 0)
}

pub struct Services {
    pub coord_port: u16,
    pub stub_port: u16,
    pub lease_secs: i32,
    #[allow(dead_code)]
    pub stub_mode: String,
    pub db_url: String,
    stub_proc: Option<Child>,
    coord_proc: Option<Child>,
}

impl Services {
    pub fn coord_url(&self) -> String {
        format!("http://127.0.0.1:{}", self.coord_port)
    }
    pub fn stub_url(&self) -> String {
        format!("http://127.0.0.1:{}", self.stub_port)
    }

    pub async fn http(&self) -> reqwest::Client {
        reqwest::Client::builder()
            .timeout(Duration::from_secs(5))
            .build()
            .unwrap()
    }

    /// 创建本用例独占的数据库并执行迁移。
    async fn provision_db(db_url: &str) {
        use sqlx::postgres::PgPoolOptions;
        let admin = PgPoolOptions::new()
            .max_connections(2)
            .connect(ADMIN_DB_URL)
            .await
            .expect("connect admin db");
        let name = db_name(db_url);
        // 若残留同名库则先删（CREATE DATABASE 不能在事务里跑）
        let exists: bool =
            sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = $1)")
                .bind(name)
                .fetch_one(&admin)
                .await
                .unwrap();
        if exists {
            sqlx::query(&format!("DROP DATABASE {name}"))
                .execute(&admin)
                .await
                .ok();
        }
        sqlx::query(&format!("CREATE DATABASE {name}"))
            .execute(&admin)
            .await
            .expect("create per-test database");
        admin.close().await;

        let pool = PgPoolOptions::new()
            .max_connections(5)
            .connect(db_url)
            .await
            .expect("connect per-test database");
        sqlx::migrate!("./migrations").run(&pool).await.expect("migrate");
    }

    pub async fn start(lease_secs: i32, stub_mode: &str) -> Self {
        // 每用例独占数据库 + 独占桩/协调器进程，端口由 OS 分配
        let db_url = per_test_db_url();
        Self::provision_db(&db_url).await;

        let (stub, stub_port) = spawn_and_port(
            &[
                "stub",
                "--bind",
                "127.0.0.1:0",
                "--secret",
                SECRET,
                "--mode",
                stub_mode,
            ],
            "listening on http://",
        )
        .await;

        let dbu = db_url.clone();
        let (coord, coord_port) = spawn_and_port(
            &[
                "serve",
                "--database-url",
                &dbu,
                "--bind",
                "127.0.0.1:0",
                "--lease-secs",
                &lease_secs.to_string(),
            ],
            "listening on http://",
        )
        .await;

        let s = Services {
            coord_port,
            stub_port,
            lease_secs,
            stub_mode: stub_mode.to_string(),
            db_url,
            stub_proc: Some(stub),
            coord_proc: Some(coord),
        };
        s.wait_ready().await;
        s
    }

    pub async fn wait_ready(&self) {
        let http = reqwest::Client::new();
        let cu = self.coord_url();
        let su = self.stub_url();
        wait_until(
            || async {
                http.get(format!("{cu}/healthz")).send().await.is_ok()
                    && http.get(format!("{su}/healthz")).send().await.is_ok()
            },
            40,
            "services ready",
        )
        .await;
    }

    pub fn coord_kill(&mut self) {
        if let Some(mut p) = self.coord_proc.take() {
            let _ = p.kill();
            let _ = p.wait();
        }
    }

    pub async fn restart_coordinator(&mut self) {
        let dbu = self.db_url.clone();
        let (coord, port) = spawn_and_port(
            &[
                "serve",
                "--database-url",
                &dbu,
                "--bind",
                "127.0.0.1:0",
                "--lease-secs",
                &self.lease_secs.to_string(),
            ],
            "listening on http://",
        )
        .await;
        self.coord_proc = Some(coord);
        self.coord_port = port;
        let url = self.coord_url();
        wait_until(
            || async {
                reqwest::Client::new()
                    .get(format!("{url}/healthz"))
                    .send()
                    .await
                    .is_ok()
            },
            40,
            "coordinator back",
        )
        .await;
    }

    pub fn relay(&self, relay_id: &str, extra: &[&str]) -> Child {
        let coord = self.coord_url();
        let target = self.stub_url();
        let lease = self.lease_secs.to_string();
        // 默认空闲 5s 自动退出，杜绝跨用例孤儿进程抢任务
        let mut args: Vec<&str> = vec![
            "relay",
            "--coordinator",
            &coord,
            "--target",
            &target,
            "--secret",
            SECRET,
            "--lease-secs",
            &lease,
            "--relay-id",
            relay_id,
            "--exit-if-idle-secs",
            "5",
        ];
        args.extend(extra.iter().copied());
        Command::new(bin())
            .args(args)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn relay")
    }
}

impl Drop for Services {
    fn drop(&mut self) {
        if let Some(mut p) = self.stub_proc.take() {
            let _ = p.kill();
            let _ = p.wait();
        }
        if let Some(mut p) = self.coord_proc.take() {
            let _ = p.kill();
            let _ = p.wait();
        }
        // 同步删除本用例数据库（Drop 是同步的，用一次性 runtime）
        let url = self.db_url.clone();
        let name = db_name(&url).to_string();
        let _ = std::thread::spawn(move || {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .unwrap();
            rt.block_on(async move {
                use sqlx::postgres::PgPoolOptions;
                if let Ok(admin) = PgPoolOptions::new()
                    .max_connections(2)
                    .connect(ADMIN_DB_URL)
                    .await
                {
                    let _ = sqlx::query(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                         WHERE datname = $1 AND pid <> pg_backend_pid()",
                    )
                    .bind(&name)
                    .execute(&admin)
                    .await;
                    let _ = sqlx::query(&format!("DROP DATABASE IF EXISTS {name}"))
                        .execute(&admin)
                        .await;
                }
            });
        })
        .join();
    }
}

pub fn kill(mut c: Child) {
    let _ = c.kill();
    let _ = c.wait();
}

pub async fn wait_until<F, Fut>(mut f: F, tries: u32, what: &str)
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = bool>,
{
    for i in 0..tries {
        if f().await {
            return;
        }
        if i == tries - 1 {
            panic!("timeout waiting for: {what}");
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
}

pub async fn enqueue(
    http: &reqwest::Client,
    svc: &Services,
    channel: &str,
    commits: Value,
) -> Value {
    http.post(format!("{}/enqueue", svc.coord_url()))
        .json(&json!({"channel_id": channel, "commits": commits}))
        .send()
        .await
        .expect("enqueue")
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap()
}

pub async fn get_sub(http: &reqwest::Client, svc: &Services, id: &str) -> Value {
    http.get(format!("{}/submissions/{id}", svc.coord_url()))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap()
}

pub async fn stub_entries(http: &reqwest::Client, svc: &Services) -> Vec<Value> {
    http.get(format!("{}/admin/entries", svc.stub_url()))
        .send()
        .await
        .unwrap()
        .json::<Value>()
        .await
        .unwrap()["entries"]
        .as_array()
        .cloned()
        .unwrap_or_default()
}

/// 等待某提交在协调器上达到指定状态。
pub async fn wait_status(
    http: &reqwest::Client,
    svc: &Services,
    id: &str,
    status: &str,
    tries: u32,
) -> Value {
    let mut last = Value::Null;
    for _ in 0..tries {
        last = get_sub(http, svc, id).await;
        if last["status"].as_str() == Some(status) {
            return last;
        }
        tokio::time::sleep(Duration::from_millis(300)).await;
    }
    panic!("submission {id} never reached {status}; last={last}");
}

/// 每测试唯一前缀（配合独立桩进程，双重隔离）。
pub fn unique(prefix: &str) -> String {
    use std::sync::atomic::{AtomicU64, Ordering};
    static N: AtomicU64 = AtomicU64::new(0);
    let n = N.fetch_add(1, Ordering::Relaxed);
    format!(
        "{prefix}-{}-{n}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    )
}
