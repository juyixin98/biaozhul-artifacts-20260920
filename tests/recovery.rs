//! 场景五：恢复 —— 中继与协调器在消息处理中途宕机/重启，状态从 PostgreSQL 恢复。

mod common;

use std::time::Duration;

fn recovery_db_url() -> String {
    // 与其他测试使用不同的库（cargo 并行跑多个测试二进制）。
    std::env::var("RECOVERY_DATABASE_URL")
        .unwrap_or_else(|_| "postgres://admin:relay_coord_dev@localhost/relay_coord_recovery".to_string())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn coordinator_restart_picks_up_leased_message_after_lease_expiry() {
    // 自建测试库。
    {
        let admin = sqlx::postgres::PgPoolOptions::new()
            .connect("postgres://admin:relay_coord_dev@localhost/postgres")
            .await
            .unwrap();
        sqlx::query("CREATE DATABASE relay_coord_recovery")
            .execute(&admin)
            .await
            .ok();
    }

    let store = std::sync::Arc::new(relay_coord::db::Store::connect(&recovery_db_url()).await.unwrap());
    store.migrate().await.unwrap();
    sqlx::raw_sql(
        "TRUNCATE TABLE messages, channels, fencing_seq RESTART IDENTITY CASCADE;
         INSERT INTO fencing_seq (id, value) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;",
    )
    .execute(&store.pool)
    .await
    .unwrap();

    let target_listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let target_port = target_listener.local_addr().unwrap().port();
    let target_url = format!("http://127.0.0.1:{target_port}");
    let target_state = relay_coord::target::StubState::new();
    tokio::spawn(async move {
        axum::serve(
            target_listener,
            relay_coord::target::shared_router(target_state),
        )
        .await
        .unwrap();
    });

    // 第一代协调器进程（真实二进制），用独立端口/独立数据库 schema 与其他测试隔离。
    let coord_bin = env!("CARGO_BIN_EXE_coordinator");
    let worker_bin = env!("CARGO_BIN_EXE_worker");

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let url = format!("http://127.0.0.1:{port}");

    let mut coord = std::process::Command::new(coord_bin)
        .args(["--listen", &format!("127.0.0.1:{port}")])
        .env("DATABASE_URL", recovery_db_url())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .unwrap();
    wait_http(&format!("{url}/healthz"), Duration::from_secs(10)).await;

    let client = relay_coord::client::Client::new(&url, Duration::from_secs(10));
    let (_, msg) = client
        .post(
            "/v1/messages",
            serde_json::json!({"channel_id": "ch-restart", "payload": {"k": "v"}}),
        )
        .await
        .unwrap();
    let id = msg["submission_id"].as_str().unwrap().to_string();

    // 用 2 秒短租约领取，然后什么都不做（模拟中继领走后崩溃）。
    client
        .post_opt(
            "/v1/leases/acquire?relay_id=dead-relay&ttl_secs=2",
            serde_json::json!({}),
        )
        .await
        .unwrap()
        .unwrap();

    // 协调器自己也宕机；数据库里留着一条 status=leased 的孤儿消息。
    coord.kill().unwrap();
    coord.wait().unwrap();

    // 重启协调器，再起一个新 worker：租约过期后消息被重新领取并确认。
    let mut coord2 = std::process::Command::new(coord_bin)
        .args(["--listen", &format!("127.0.0.1:{port}")])
        .env("DATABASE_URL", recovery_db_url())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .unwrap();
    wait_http(&format!("{url}/healthz"), Duration::from_secs(10)).await;

    let mut worker = std::process::Command::new(worker_bin)
        .args([
            "--coordinator",
            &url,
            "--target",
            &target_url,
            "--relay-id",
            "reborn-relay",
            "--lease-ttl",
            "10",
            "--submit-timeout",
            "2",
            "--poll-ms",
            "100",
        ])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .unwrap();

    let client2 = relay_coord::client::Client::new(&url, Duration::from_secs(10));
    let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
    let mut final_msg = None;
    while tokio::time::Instant::now() < deadline {
        if let Ok((200, v)) = client2.get(&format!("/v1/messages/{id}")).await {
            if v["status"] == "confirmed" {
                final_msg = Some(v);
                break;
            }
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    let v = final_msg.expect("重启后消息应被恢复并确认");
    assert_eq!(v["status"], "confirmed");
    assert!(v["attempts"].as_i64().unwrap() >= 2, "经历过两代领取");
    assert!(v["target_tx_id"].as_str().unwrap().starts_with("0x"));

    worker.kill().unwrap();
    let _ = worker.wait();
    coord2.kill().unwrap();
    let _ = coord2.wait();
}

async fn wait_http(url: &str, timeout: Duration) {
    let deadline = tokio::time::Instant::now() + timeout;
    let http = reqwest::Client::new();
    while tokio::time::Instant::now() < deadline {
        if http.get(url).send().await.is_ok() {
            return;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    panic!("{url} 未在 {timeout:?} 内就绪");
}
