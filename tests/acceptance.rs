//! 验收测试：通过真实 HTTP 接口并发注入三类冲突与环状等待，
//! 验证死锁检测在限定时间内触发、只中止一个事务、中止后重放可完成，
//! 并校验等待链/误杀统计与无锁泄漏。

use lockmgr::api;
use lockmgr::lock_manager::{Config, LockManager};
use reqwest::{Client, StatusCode};
use serde_json::{json, Value};
use std::sync::Arc;
use std::time::{Duration, Instant};

struct App {
    base: String,
    http: Client,
}

async fn spawn(config: Config) -> App {
    let mgr = Arc::new(LockManager::new(config));
    let app = api::router(mgr);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    App {
        base: format!("http://{addr}"),
        http: Client::new(),
    }
}

impl App {
    async fn begin(&self) -> (StatusCode, Value) {
        let r = self
            .http
            .post(format!("{}/tx", self.base))
            .send()
            .await
            .unwrap();
        (r.status(), r.json().await.unwrap())
    }

    async fn begin_ok(&self) -> u64 {
        let (st, v) = self.begin().await;
        assert_eq!(st, StatusCode::OK, "begin failed: {v}");
        v["tx_id"].as_u64().unwrap()
    }

    async fn lock(
        &self,
        tx: u64,
        resource: &str,
        mode: &str,
        timeout_ms: Option<u64>,
    ) -> (StatusCode, Value) {
        let mut body = json!({ "resource": resource, "mode": mode });
        if let Some(t) = timeout_ms {
            body["timeout_ms"] = json!(t);
        }
        let r = self
            .http
            .post(format!("{}/tx/{tx}/locks", self.base))
            .json(&body)
            .send()
            .await
            .unwrap();
        (r.status(), r.json().await.unwrap())
    }

    async fn lock_ok(&self, tx: u64, resource: &str, mode: &str) {
        let (st, v) = self.lock(tx, resource, mode, Some(10_000)).await;
        assert_eq!(st, StatusCode::OK, "lock {resource} failed: {v}");
    }

    async fn commit(&self, tx: u64) -> StatusCode {
        self.http
            .post(format!("{}/tx/{tx}/commit", self.base))
            .send()
            .await
            .unwrap()
            .status()
    }

    async fn metrics(&self) -> Value {
        self.http
            .get(format!("{}/metrics", self.base))
            .send()
            .await
            .unwrap()
            .json()
            .await
            .unwrap()
    }

    async fn graph(&self) -> Vec<(u64, u64)> {
        let v: Value = self
            .http
            .get(format!("{}/graph", self.base))
            .send()
            .await
            .unwrap()
            .json()
            .await
            .unwrap();
        v["wait_for_edges"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| (e["from"].as_u64().unwrap(), e["to"].as_u64().unwrap()))
            .collect()
    }

    async fn wait_edge(&self, from: u64, to: u64) {
        for _ in 0..300 {
            if self.graph().await.contains(&(from, to)) {
                return;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        panic!("edge {from}->{to} never appeared");
    }

    async fn assert_no_leak(&self) {
        let m = self.metrics().await;
        assert_eq!(m["current_holders"], 0, "lock leak: holders left");
        assert_eq!(m["current_waiters"], 0, "lock leak: waiters left");
        assert_eq!(m["wait_graph_edges"], 0, "wait-for graph leak");
    }
}

/// 三类冲突：S/X、X/X、X/S（以及 S/S 兼容对照）。
#[tokio::test]
async fn three_conflict_types() {
    let app = spawn(Config::default()).await;

    // 对照：S+S 兼容
    let t1 = app.begin_ok().await;
    let t2 = app.begin_ok().await;
    app.lock_ok(t1, "R0", "shared").await;
    app.lock_ok(t2, "R0", "shared").await;
    app.commit(t1).await;
    app.commit(t2).await;

    // 冲突一：S 持有 vs X 请求
    let t1 = app.begin_ok().await;
    let t2 = app.begin_ok().await;
    app.lock_ok(t1, "R1", "shared").await;
    let (st, _) = app.lock(t2, "R1", "exclusive", Some(200)).await;
    assert_eq!(st, StatusCode::REQUEST_TIMEOUT, "S/X should conflict");
    app.commit(t1).await;

    // 冲突二：X 持有 vs X 请求
    let t1 = app.begin_ok().await;
    let t2 = app.begin_ok().await;
    app.lock_ok(t1, "R2", "exclusive").await;
    let (st, _) = app.lock(t2, "R2", "exclusive", Some(200)).await;
    assert_eq!(st, StatusCode::REQUEST_TIMEOUT, "X/X should conflict");
    app.commit(t1).await;

    // 冲突三：X 持有 vs S 请求
    let t1 = app.begin_ok().await;
    let t2 = app.begin_ok().await;
    app.lock_ok(t1, "R3", "exclusive").await;
    let (st, _) = app.lock(t2, "R3", "shared", Some(200)).await;
    assert_eq!(st, StatusCode::REQUEST_TIMEOUT, "X/S should conflict");
    app.commit(t1).await;

    let m = app.metrics().await;
    assert_eq!(m["timeout_aborts_false_kill"], 3);
    app.assert_no_leak().await;
}

/// 两事务环状等待：检测须在限定时间内触发，只中止一个事务，重放可完成。
#[tokio::test]
async fn two_tx_cycle_detection_and_replay() {
    let app = spawn(Config::default()).await;
    let t1 = app.begin_ok().await;
    let t2 = app.begin_ok().await;
    app.lock_ok(t1, "A", "exclusive").await;
    app.lock_ok(t2, "B", "exclusive").await;

    // T1 等待 B（后台挂起）
    let app_base = app.base.clone();
    let http = app.http.clone();
    let h = tokio::spawn(async move {
        let r = http
            .post(format!("{app_base}/tx/{t1}/locks"))
            .json(&json!({ "resource": "B", "mode": "exclusive", "timeout_ms": 10_000 }))
            .send()
            .await
            .unwrap();
        (r.status(), r.json::<Value>().await.unwrap())
    });
    app.wait_edge(t1, t2).await;

    // T2 请求 A -> 成环，检测应几乎立即触发
    let start = Instant::now();
    let (st, body) = app.lock(t2, "A", "exclusive", Some(10_000)).await;
    let detect_latency = start.elapsed();
    assert_eq!(st, StatusCode::CONFLICT, "victim should get 409: {body}");
    assert_eq!(body["error"], "deadlock_victim");
    assert!(
        body["detail"].as_str().unwrap().contains(&format!("victim T{t2}")),
        "victim must be the youngest (largest txid): {body}"
    );
    assert!(
        detect_latency < Duration::from_secs(2),
        "detection too slow: {detect_latency:?}"
    );

    // 只中止一个：T1 的等待应被满足
    let (st1, _) = h.await.unwrap();
    assert_eq!(st1, StatusCode::OK, "survivor T1 must be granted");
    assert_eq!(app.commit(t1).await, StatusCode::OK);

    let m = app.metrics().await;
    assert_eq!(m["deadlocks_detected"], 1);
    assert_eq!(m["deadlock_aborts"], 1);

    // 中止后重放事务必须能完成
    let t3 = app.begin_ok().await;
    app.lock_ok(t3, "A", "exclusive").await;
    app.lock_ok(t3, "B", "exclusive").await;
    assert_eq!(app.commit(t3).await, StatusCode::OK);

    app.assert_no_leak().await;
}

/// 三事务环：T1->T2->T3->T1，只中止最年轻者 T3，其余完成。
#[tokio::test]
async fn three_tx_cycle() {
    let app = spawn(Config::default()).await;
    let t1 = app.begin_ok().await;
    let t2 = app.begin_ok().await;
    let t3 = app.begin_ok().await;
    app.lock_ok(t1, "A", "exclusive").await;
    app.lock_ok(t2, "B", "exclusive").await;
    app.lock_ok(t3, "C", "exclusive").await;

    // T1 等 B（T2 持有）
    let base = app.base.clone();
    let http = app.http.clone();
    let h1 = tokio::spawn(async move {
        http.post(format!("{base}/tx/{t1}/locks"))
            .json(&json!({ "resource": "B", "mode": "exclusive", "timeout_ms": 10_000 }))
            .send()
            .await
            .unwrap()
            .status()
    });
    app.wait_edge(t1, t2).await;

    // T2 等 C（T3 持有）
    let base = app.base.clone();
    let http = app.http.clone();
    let h2 = tokio::spawn(async move {
        http.post(format!("{base}/tx/{t2}/locks"))
            .json(&json!({ "resource": "C", "mode": "exclusive", "timeout_ms": 10_000 }))
            .send()
            .await
            .unwrap()
            .status()
    });
    app.wait_edge(t2, t3).await;

    // T3 等 A（T1 持有）-> 成环
    let (st, body) = app.lock(t3, "A", "exclusive", Some(10_000)).await;
    assert_eq!(st, StatusCode::CONFLICT);
    assert!(body["detail"]
        .as_str()
        .unwrap()
        .contains(&format!("victim T{t3}")));

    // T3 中止释放 C -> T2 获得 C；T2 提交 -> T1 获得 B
    assert_eq!(h2.await.unwrap(), StatusCode::OK);
    assert_eq!(app.commit(t2).await, StatusCode::OK);
    assert_eq!(h1.await.unwrap(), StatusCode::OK);
    assert_eq!(app.commit(t1).await, StatusCode::OK);

    let m = app.metrics().await;
    assert_eq!(m["deadlocks_detected"], 1);
    assert_eq!(m["deadlock_aborts"], 1);
    assert!(m["wait_chain_max"].as_u64().unwrap() >= 2);
    app.assert_no_leak().await;
}

/// 锁升级：单持有者可就地升级；两事务同时升级构成死锁。
#[tokio::test]
async fn lock_upgrade_and_upgrade_deadlock() {
    let app = spawn(Config::default()).await;

    // 就地升级
    let t1 = app.begin_ok().await;
    app.lock_ok(t1, "U", "shared").await;
    app.lock_ok(t1, "U", "exclusive").await;
    app.commit(t1).await;

    // 升级死锁：T1、T2 各持 S，同时请求 X
    let t1 = app.begin_ok().await;
    let t2 = app.begin_ok().await;
    app.lock_ok(t1, "V", "shared").await;
    app.lock_ok(t2, "V", "shared").await;
    let base = app.base.clone();
    let http = app.http.clone();
    let h = tokio::spawn(async move {
        http.post(format!("{base}/tx/{t1}/locks"))
            .json(&json!({ "resource": "V", "mode": "exclusive", "timeout_ms": 10_000 }))
            .send()
            .await
            .unwrap()
            .status()
    });
    app.wait_edge(t1, t2).await;
    let (st, _) = app.lock(t2, "V", "exclusive", Some(10_000)).await;
    assert_eq!(st, StatusCode::CONFLICT, "younger upgrader must be victim");
    assert_eq!(h.await.unwrap(), StatusCode::OK, "survivor upgrade granted");
    app.commit(t1).await;

    let m = app.metrics().await;
    assert_eq!(m["deadlock_aborts"], 1);
    app.assert_no_leak().await;
}

/// 锁超时：超时事务被中止并计入误杀（非确认死锁）。
#[tokio::test]
async fn lock_timeout_counts_as_false_kill() {
    let app = spawn(Config::default()).await;
    let t1 = app.begin_ok().await;
    let t2 = app.begin_ok().await;
    app.lock_ok(t1, "A", "exclusive").await;
    let start = Instant::now();
    let (st, _) = app.lock(t2, "A", "exclusive", Some(300)).await;
    assert_eq!(st, StatusCode::REQUEST_TIMEOUT);
    assert!(start.elapsed() >= Duration::from_millis(300));
    // 超时事务已中止，后续操作应为 410
    let (st2, _) = app.lock(t2, "B", "shared", Some(100)).await;
    assert_eq!(st2, StatusCode::GONE);
    app.commit(t1).await;
    let m = app.metrics().await;
    assert_eq!(m["timeout_aborts_false_kill"], 1);
    assert_eq!(m["deadlocks_detected"], 0);
    app.assert_no_leak().await;
}

/// 等待图规模上限：达到上限拒绝新事务并输出告警指标。
#[tokio::test]
async fn wait_graph_cap_rejects_and_alerts() {
    let app = spawn(Config {
        max_wait_edges: 1,
        ..Config::default()
    })
    .await;
    let t1 = app.begin_ok().await;
    let t2 = app.begin_ok().await;
    app.lock_ok(t1, "A", "exclusive").await;

    // T2 等 A：占用唯一一条边
    let base = app.base.clone();
    let http = app.http.clone();
    let h = tokio::spawn(async move {
        http.post(format!("{base}/tx/{t2}/locks"))
            .json(&json!({ "resource": "A", "mode": "exclusive", "timeout_ms": 10_000 }))
            .send()
            .await
            .unwrap()
            .status()
    });
    app.wait_edge(t2, t1).await;

    // 新事务被拒绝（503）且告警指标增加
    let (st, body) = app.begin().await;
    assert_eq!(st, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(body["error"], "wait_graph_full");

    // T1 释放后 T2 获得锁，图清空，新事务恢复可用
    app.commit(t1).await;
    assert_eq!(h.await.unwrap(), StatusCode::OK);
    app.commit(t2).await;
    let t3 = app.begin_ok().await;
    app.commit(t3).await;

    let m = app.metrics().await;
    assert!(m["wait_graph_full_rejections"].as_u64().unwrap() >= 1);
    app.assert_no_leak().await;
}

/// 经 FIFO 等待队列闭合的环：S 持有者 T1、T3 持 X(B)；
/// T2 排队等 X(A)，T3 在其后排队等 S(A)（与 T1 的 S 兼容，只被 T2 阻塞）；
/// T1 再请求 X(B) 成环。要求即时检测（<2s）、只中止最大 txid 的 T3、零误杀。
#[tokio::test]
async fn queue_mediated_cycle_detected_without_false_kill() {
    let app = spawn(Config::default()).await;
    let t1 = app.begin_ok().await;
    let t2 = app.begin_ok().await;
    let t3 = app.begin_ok().await;
    app.lock_ok(t1, "A", "shared").await;
    app.lock_ok(t3, "B", "exclusive").await;

    let (base, http) = (app.base.clone(), app.http.clone());
    let h2 = tokio::spawn(async move {
        http.post(format!("{base}/tx/{t2}/locks"))
            .json(&json!({ "resource": "A", "mode": "exclusive", "timeout_ms": 10_000 }))
            .send()
            .await
            .unwrap()
            .status()
    });
    app.wait_edge(t2, t1).await;

    let (base, http) = (app.base.clone(), app.http.clone());
    let h3 = tokio::spawn(async move {
        http.post(format!("{base}/tx/{t3}/locks"))
            .json(&json!({ "resource": "A", "mode": "shared", "timeout_ms": 10_000 }))
            .send()
            .await
            .unwrap()
            .status()
    });
    app.wait_edge(t3, t2).await;

    let start = Instant::now();
    let (st, body) = app.lock(t1, "B", "exclusive", Some(10_000)).await;
    assert_eq!(st, StatusCode::OK, "T1 must be granted B after victim abort: {body}");
    assert!(
        start.elapsed() < Duration::from_secs(2),
        "queue-mediated cycle detection too slow"
    );
    assert_eq!(h3.await.unwrap(), StatusCode::CONFLICT, "T3 is victim");

    // T1 释放 A 后 T2 获得
    assert_eq!(app.commit(t1).await, StatusCode::OK);
    assert_eq!(h2.await.unwrap(), StatusCode::OK);
    assert_eq!(app.commit(t2).await, StatusCode::OK);

    let m = app.metrics().await;
    assert_eq!(m["deadlocks_detected"], 1);
    assert_eq!(m["deadlock_aborts"], 1);
    assert_eq!(m["timeout_aborts_false_kill"], 0);

    // 重放被中止的 T3 的工作负载（拿 A、B 后提交）
    let t4 = app.begin_ok().await;
    app.lock_ok(t4, "A", "exclusive").await;
    app.lock_ok(t4, "B", "exclusive").await;
    assert_eq!(app.commit(t4).await, StatusCode::OK);

    app.assert_no_leak().await;
}

/// 报告端点：包含等待链与误杀统计。
#[tokio::test]
async fn report_endpoint_outputs_stats() {
    let app = spawn(Config::default()).await;
    let t1 = app.begin_ok().await;
    app.lock_ok(t1, "A", "exclusive").await;
    app.commit(t1).await;
    let text = app
        .http
        .get(format!("{}/report", app.base))
        .send()
        .await
        .unwrap()
        .text()
        .await
        .unwrap();
    assert!(text.contains("wait chain length"));
    assert!(text.contains("false-kill"));
    assert!(text.contains("deadlocks detected"));
}
