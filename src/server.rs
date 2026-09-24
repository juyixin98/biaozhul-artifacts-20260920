use crate::arbiter::evaluate;
use crate::clock::{Clock, ManualClock, SystemClock};
use crate::config::{Config, SourceKind};
use crate::crypto::{verify_header, SigDomain};
use crate::db::Db;
use crate::error::{AppError, Reject};
use crate::models::*;
use axum::body::Bytes;
use axum::extract::{Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Deserialize;
use std::sync::Arc;

/// 共享应用状态。
pub struct AppState {
    pub cfg: Config,
    pub db: Db,
    pub clock: Arc<dyn Clock>,
    /// 把“接收 -> 校验 -> 落库 -> 仲裁 -> 记决策”整个收命令临界区串行化，
    /// 保证同刻竞争下的结果可复现（数据库本身也被同一互斥锁保护）。
    pub ingest_lock: std::sync::Mutex<()>,
}

pub fn build_router(state: Arc<AppState>) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/v1/time", get(get_time))
        .route("/v1/command", post(post_command))
        .route("/v1/estop", post(post_estop))
        .route("/v1/evaluate", post(post_evaluate))
        .route("/v1/decision/latest", get(get_latest_decision))
        .route("/v1/decisions/history", get(get_history))
        .route("/admin/state", get(get_admin_state))
        .route("/admin/clock/advance", post(post_clock_advance))
        .with_state(state)
}

pub async fn start_server(cfg: Config, db: Db, clock: Arc<dyn Clock>, addr: &str) {
    let state = Arc::new(AppState {
        cfg,
        db,
        clock,
        ingest_lock: std::sync::Mutex::new(()),
    });
    let app = build_router(state);
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .expect("绑定监听地址失败");
    tracing::info!("运动命令仲裁服务监听 {addr}");
    axum::serve(
        listener,
        app.into_make_service_with_connect_info::<std::net::SocketAddr>(),
    )
    .with_graceful_shutdown(shutdown_signal())
    .await
    .expect("服务器异常退出");
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    tracing::info!("收到 Ctrl-C，正在关闭");
}

// ---------- 基本工具 ----------

fn err(rej: Reject, detail: impl Into<String>) -> AppError {
    AppError::new(rej, detail)
}

fn header_str<'a>(headers: &'a HeaderMap, name: &str) -> Option<&'a str> {
    headers.get(name).and_then(|v| v.to_str().ok())
}

/// 鉴权：来源必须存在、签名必须通过。签名覆盖原始 body 字节。
fn authenticate(
    state: &AppState,
    headers: &HeaderMap,
    body: &[u8],
    domain: SigDomain,
) -> Result<(String, SourceKind), AppError> {
    let source = header_str(headers, "x-source")
        .ok_or_else(|| err(Reject::BadSignatureHeader, "缺少 X-Source 头"))?
        .to_string();
    let cfg = state
        .cfg
        .source(&source)
        .ok_or_else(|| err(Reject::UnknownSource, format!("未知来源: {source}")))?;
    let kind = cfg.kind;
    let key = state
        .cfg
        .key_bytes(&source)
        .ok_or_else(|| err(Reject::UnknownSource, "密钥不可用"))?;
    let sig = header_str(headers, "x-signature");
    verify_header(&key, domain, body, sig).map_err(|_| {
        err(
            Reject::BadSignature,
            "HMAC-SHA256 签名校验失败（应为 X-Signature: sha256=<hex>，签名内容为域前缀+原始请求体）",
        )
    })?;
    Ok((source, kind))
}

fn compute_decision(state: &AppState, at_ms: i64) -> Result<DecisionOutcome, AppError> {
    let world = state
        .db
        .load_world()
        .map_err(|e| err(Reject::Internal, e))?;
    Ok(evaluate(&world, at_ms, &state.cfg.limits))
}

fn truncate(s: &str) -> String {
    s.chars().take(4096).collect()
}

// ---------- handlers ----------

async fn healthz() -> &'static str {
    "ok"
}

async fn get_time(State(state): State<Arc<AppState>>) -> Json<ClockResponse> {
    Json(ClockResponse {
        mode: state.clock.mode().into(),
        now_ms: state.clock.now_ms(),
    })
}

async fn post_command(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<(StatusCode, Json<IngestResponse>), AppError> {
    // 鉴权先于 JSON 解析：签名的是原始字节。
    let (source, kind) = authenticate(&state, &headers, &body, SigDomain::Motion)?;
    let req: CommandReq = serde_json::from_slice(&body)
        .map_err(|e| err(Reject::BadJson, format!("请求体不是合法 JSON: {e}")))?;

    // 只有 autonomous / rc 可以往这里发。
    if kind == SourceKind::Estop {
        return Err(err(
            Reject::BadPayload,
            "estop 类型来源必须使用 /v1/estop 接口",
        ));
    }

    validate_scalar(&req.nonce, req.issue_ms, req.valid_for_ms)?;
    if req.lease_for_ms <= 0 {
        return Err(err(Reject::BadPayload, "lease_for_ms 必须为正"));
    }
    for (name, v) in [("vx", req.vx), ("vy", req.vy), ("omega", req.omega)] {
        if !v.is_finite() {
            return Err(err(Reject::BadPayload, format!("{name} 必须是有限数值")));
        }
    }

    let _guard = state.ingest_lock.lock().unwrap();
    let now = state.clock.now_ms();
    let raw = truncate(&String::from_utf8_lossy(&body));

    // 有效期窗口（过期/超前）。
    if now > req.issue_ms + req.valid_for_ms {
        reject(
            &state,
            now,
            "/v1/command",
            &source,
            Some(req.seq),
            Some(&req.nonce),
            Reject::Expired,
            &format!(
                "命令已过有效期：now={now} > issue_ms({}) + valid_for_ms({})",
                req.issue_ms, req.valid_for_ms
            ),
            &raw,
        )?;
    }
    if now < req.issue_ms - state.cfg.max_future_ms {
        reject(
            &state,
            now,
            "/v1/command",
            &source,
            Some(req.seq),
            Some(&req.nonce),
            Reject::TooFarInFuture,
            &format!("issue_ms 超前服务器时间超过 {}ms", state.cfg.max_future_ms),
            &raw,
        )?;
    }
    // 租约上限。
    if req.lease_for_ms > state.cfg.max_lease_ms {
        reject(
            &state,
            now,
            "/v1/command",
            &source,
            Some(req.seq),
            Some(&req.nonce),
            Reject::LeaseTooLong,
            &format!(
                "lease_for_ms={} 超过服务器上限 {}ms",
                req.lease_for_ms, state.cfg.max_lease_ms
            ),
            &raw,
        )?;
    }

    // 序号必须严格递增（按来源）。
    let max_seq = state
        .db
        .max_command_seq(&source)
        .map_err(|e| err(Reject::Internal, e))?;
    if req.seq <= max_seq {
        reject(
            &state,
            now,
            "/v1/command",
            &source,
            Some(req.seq),
            Some(&req.nonce),
            Reject::StaleSequence,
            &format!("seq={} 不新，该来源已接受的最大 seq={max_seq}", req.seq),
            &raw,
        )?;
    }
    // nonce 防重放（防御性：正常情况下 seq 检查已覆盖）。
    if state
        .db
        .command_nonce_exists(&source, &req.nonce)
        .map_err(|e| err(Reject::Internal, e))?
    {
        reject(
            &state,
            now,
            "/v1/command",
            &source,
            Some(req.seq),
            Some(&req.nonce),
            Reject::ReplayedNonce,
            "nonce 已出现过",
            &raw,
        )?;
    }

    let command = Command {
        source: source.clone(),
        source_kind: kind.as_str().into(),
        seq: req.seq,
        nonce: req.nonce.clone(),
        vx: req.vx,
        vy: req.vy,
        omega: req.omega,
        issue_ms: req.issue_ms,
        deadline_ms: req.issue_ms + req.lease_for_ms,
        received_ms: now,
    };
    state.db.insert_command(&command).map_err(|e| {
        // 唯一约束 (source, nonce) 冲突 => 重放。
        if e.contains("UNIQUE") || e.contains("constraint") {
            err(Reject::ReplayedNonce, e)
        } else {
            err(Reject::Internal, e)
        }
    })?;

    let decision = compute_decision(&state, now)?;
    state
        .db
        .insert_decision(now, "command", &decision)
        .map_err(|e| err(Reject::Internal, e))?;
    state
        .db
        .log_ingest(
            now,
            "/v1/command",
            Some(&source),
            Some(req.seq),
            Some(&req.nonce),
            true,
            None,
            None,
            &raw,
        )
        .map_err(|e| err(Reject::Internal, e))?;

    Ok((
        StatusCode::ACCEPTED,
        Json(IngestResponse {
            accepted: true,
            received_ms: now,
            decision,
        }),
    ))
}

async fn post_estop(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<(StatusCode, Json<IngestResponse>), AppError> {
    let (source, kind) = authenticate(&state, &headers, &body, SigDomain::Estop)?;
    let req: EstopReq = serde_json::from_slice(&body)
        .map_err(|e| err(Reject::BadJson, format!("请求体不是合法 JSON: {e}")))?;

    if kind != SourceKind::Estop {
        return Err(err(
            Reject::BadPayload,
            "只有 estop 类型来源可以调用 /v1/estop",
        ));
    }
    if req.action != "trigger" && req.action != "clear" {
        return Err(err(
            Reject::BadPayload,
            "action 必须是 \"trigger\" 或 \"clear\"",
        ));
    }
    validate_scalar(&req.nonce, req.issue_ms, req.valid_for_ms)?;

    let _guard = state.ingest_lock.lock().unwrap();
    let now = state.clock.now_ms();
    let raw = truncate(&String::from_utf8_lossy(&body));

    if now > req.issue_ms + req.valid_for_ms {
        reject(
            &state,
            now,
            "/v1/estop",
            &source,
            Some(req.seq),
            Some(&req.nonce),
            Reject::Expired,
            &format!(
                "急停消息已过有效期：now={now} > issue_ms({}) + valid_for_ms({})",
                req.issue_ms, req.valid_for_ms
            ),
            &raw,
        )?;
    }
    if now < req.issue_ms - state.cfg.max_future_ms {
        reject(
            &state,
            now,
            "/v1/estop",
            &source,
            Some(req.seq),
            Some(&req.nonce),
            Reject::TooFarInFuture,
            &format!("issue_ms 超前服务器时间超过 {}ms", state.cfg.max_future_ms),
            &raw,
        )?;
    }

    let max_seq = state
        .db
        .max_estop_seq(&source)
        .map_err(|e| err(Reject::Internal, e))?;
    if req.seq <= max_seq {
        reject(
            &state,
            now,
            "/v1/estop",
            &source,
            Some(req.seq),
            Some(&req.nonce),
            Reject::StaleSequence,
            &format!("seq={} 不新，该急停来源已接受的最大 seq={max_seq}", req.seq),
            &raw,
        )?;
    }
    if state
        .db
        .estop_nonce_exists(&source, &req.nonce)
        .map_err(|e| err(Reject::Internal, e))?
    {
        reject(
            &state,
            now,
            "/v1/estop",
            &source,
            Some(req.seq),
            Some(&req.nonce),
            Reject::ReplayedNonce,
            "nonce 已出现过",
            &raw,
        )?;
    }

    // “解除”只在当前锁存时有意义；对未锁存的 clear 明确拒绝。
    if req.action == "clear" {
        let latched = state
            .db
            .last_estop_action()
            .map_err(|e| err(Reject::Internal, e))?
            .as_deref()
            == Some("trigger");
        if !latched {
            reject(
                &state,
                now,
                "/v1/estop",
                &source,
                Some(req.seq),
                Some(&req.nonce),
                Reject::EstopNotActive,
                "急停当前未锁存，clear 没有意义（急停不会因 clear 以外的任何方式解除）",
                &raw,
            )?;
        }
    }

    let event = EstopEvent {
        source: source.clone(),
        seq: req.seq,
        action: req.action.clone(),
        received_ms: now,
    };
    state
        .db
        .insert_estop_event(&event, &req.nonce)
        .map_err(|e| err(Reject::Internal, e))?;

    let decision = compute_decision(&state, now)?;
    state
        .db
        .insert_decision(now, "estop", &decision)
        .map_err(|e| err(Reject::Internal, e))?;
    state
        .db
        .log_ingest(
            now,
            "/v1/estop",
            Some(&source),
            Some(req.seq),
            Some(&req.nonce),
            true,
            None,
            None,
            &raw,
        )
        .map_err(|e| err(Reject::Internal, e))?;

    Ok((
        StatusCode::ACCEPTED,
        Json(IngestResponse {
            accepted: true,
            received_ms: now,
            decision,
        }),
    ))
}

#[derive(Debug, Deserialize)]
struct EvaluateParams {
    at_ms: Option<i64>,
    persist: Option<bool>,
}

async fn post_evaluate(
    State(state): State<Arc<AppState>>,
    Json(body): Json<EvaluateParams>,
) -> Result<Json<DecisionOutcome>, AppError> {
    let now = state.clock.now_ms();
    let at = body.at_ms.unwrap_or(now);
    // 允许在手动时钟下回看/前看任意时刻做“同刻竞争”验收，但不允许篡改时钟状态。
    let persist = body.persist.unwrap_or(true);
    let decision = if persist {
        let _guard = state.ingest_lock.lock().unwrap();
        let d = compute_decision(&state, at)?;
        state
            .db
            .insert_decision(at, "evaluate", &d)
            .map_err(|e| err(Reject::Internal, e))?;
        d
    } else {
        compute_decision(&state, at)?
    };
    Ok(Json(decision))
}

/// 查询最新决策：纯读，不写任何表，绝不刷新租约。
async fn get_latest_decision(
    State(state): State<Arc<AppState>>,
) -> Result<Json<serde_json::Value>, AppError> {
    match state
        .db
        .latest_decision()
        .map_err(|e| err(Reject::Internal, e))?
    {
        Some((id, json)) => {
            let mut v: serde_json::Value =
                serde_json::from_str(&json).map_err(|e| err(Reject::Internal, e.to_string()))?;
            v["id"] = serde_json::json!(id);
            Ok(Json(v))
        }
        None => Err(err(
            Reject::NoDecision,
            "暂无决策记录（先收一条命令或调用 /v1/evaluate）",
        )),
    }
}

#[derive(Debug, Deserialize)]
struct HistoryParams {
    limit: Option<i64>,
}

async fn get_history(
    State(state): State<Arc<AppState>>,
    Query(q): Query<HistoryParams>,
) -> Result<Json<Vec<serde_json::Value>>, AppError> {
    let limit = q.limit.unwrap_or(20).clamp(1, 500);
    state
        .db
        .decision_history(limit)
        .map(Json)
        .map_err(|e| err(Reject::Internal, e))
}

async fn post_clock_advance(
    State(state): State<Arc<AppState>>,
    Json(req): Json<ClockAdvanceReq>,
) -> Result<Json<ClockResponse>, AppError> {
    if state.clock.mode() != "manual" {
        return Err(err(
            Reject::BadPayload,
            "仅手动时钟模式支持推进时间（重启服务时加 --clock manual）",
        ));
    }
    if req.delta_ms < 0 {
        return Err(err(Reject::BadPayload, "delta_ms 不能为负"));
    }
    let now = state
        .clock
        .advance(req.delta_ms)
        .map_err(|e| err(Reject::BadPayload, e))?;
    // 持久化手动时钟，使“重启后时钟继续”可验收。
    state
        .db
        .set_meta("manual_clock_ms", &now.to_string())
        .map_err(|e| err(Reject::Internal, e))?;
    Ok(Json(ClockResponse {
        mode: state.clock.mode().into(),
        now_ms: now,
    }))
}

async fn get_admin_state(State(state): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let world = state.db.load_world().unwrap_or(WorldView {
        commands: vec![],
        estop_events: vec![],
    });
    let now = state.clock.now_ms();
    let decision = evaluate(&world, now, &state.cfg.limits);
    let decision_count = state.db.count_decisions().unwrap_or(-1);
    Json(serde_json::json!({
        "clock": { "mode": state.clock.mode(), "now_ms": now },
        "limits": state.cfg.limits,
        "sources": state.cfg.sources.keys().collect::<Vec<_>>(),
        "stored_commands": world.commands.len(),
        "stored_estop_events": world.estop_events.len(),
        "decision_records": decision_count,
        "current_arbitration": decision,
    }))
}

// ---------- 校验/拒绝辅助 ----------

fn validate_scalar(nonce: &str, issue_ms: i64, valid_for_ms: i64) -> Result<(), AppError> {
    if nonce.is_empty() || nonce.len() > 128 {
        return Err(err(Reject::BadPayload, "nonce 必须为 1..=128 字节"));
    }
    if valid_for_ms <= 0 {
        return Err(err(Reject::BadPayload, "valid_for_ms 必须为正"));
    }
    if issue_ms <= 0 {
        return Err(err(Reject::BadPayload, "issue_ms 必须为正的毫秒时间戳"));
    }
    Ok(())
}

/// 记录拒绝日志并返回对应 HTTP 错误。日志落库失败不吞掉原始拒绝原因。
#[allow(clippy::too_many_arguments)]
fn reject(
    state: &AppState,
    now: i64,
    endpoint: &str,
    source: &str,
    seq: Option<i64>,
    nonce: Option<&str>,
    rej: Reject,
    detail: &str,
    raw: &str,
) -> Result<(), AppError> {
    let _ = state.db.log_ingest(
        now,
        endpoint,
        Some(source),
        seq,
        nonce,
        false,
        Some(rej.reason()),
        Some(detail),
        raw,
    );
    Err(err(rej, detail))
}

// ---------- 启动辅助 ----------

pub fn build_clock(mode: &str, db: &Db, seed_ms: Option<i64>) -> Arc<dyn Clock> {
    match mode {
        "system" => Arc::new(SystemClock),
        "manual" => {
            // 优先使用库中持久化的时钟（重启恢复）；没有时用显式 seed，
            // 再没有就取系统时间向下取整到秒。
            let persisted = db
                .get_meta("manual_clock_ms")
                .ok()
                .flatten()
                .and_then(|s| s.parse().ok());
            let seed = persisted
                .or(seed_ms)
                .unwrap_or_else(|| SystemClock.now_ms() / 1_000 * 1_000);
            Arc::new(ManualClock::new(seed))
        }
        other => panic!("未知时钟模式: {other}（支持 system | manual）"),
    }
}
