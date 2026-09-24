//! 运动命令仲裁核心：纯计算，不接触任何电机。
//!
//! 仲裁规则（评估时刻 `at`）：
//! 1. 急停锁存期间一律输出零速，所有来源命令被抑制（estop_latched）。
//! 2. 锁存解除后，只有 issued_at 严格晚于解除时刻的新命令可被接受（门控）。
//! 3. 遥控优先级高于自主。遥控失联（租约/TTL 到期或显式释放）后，
//!    门控时刻推进到遥控占用结束时刻；旧自主命令因 issued_at 过旧而被
//!    stale_after_override 抑制，必须等自主源发来新鲜命令。
//! 4. 每源序号单调，旧序号/重复序号拒绝；消息到达时已超 TTL/租约的拒绝。
//! 5. 查询（GET）与纯评估 tick 只做投影，绝不刷新任何租约或状态。

use std::sync::Mutex;

use rusqlite::params;

use crate::db::Store;
use crate::model::reason::*;
use crate::model::*;

/// 可由配置收紧的速度输入范围（防 NaN/Inf 与离谱数值）。
#[derive(Clone, Copy, Debug)]
pub struct Limits {
    pub max_vx: f64,
    pub max_wz: f64,
    /// issued_at 允许领先服务器时钟的毫秒数。
    pub future_skew_ms: i64,
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            max_vx: 10.0,
            max_wz: std::f64::consts::TAU,
            future_skew_ms: 500,
        }
    }
}

struct Inner {
    clock_mode: ClockMode,
    /// sim 模式下最近一次写入请求携带的时刻；水合自 DB。wall 模式不使用。
    sim_now: i64,
    estop: EstopState,
    autonomous: SourceState,
    remote: SourceState,
    /// 最近一次急停解除时刻；所有来源的命令 issued_at 必须严格晚于它。
    freshness_gate: i64,
    /// 最近一次遥控“接管起点”：RC 命令的 issued_at（RC 到达时推进），
    /// 或 RC 显式释放租约的时刻。自主命令 issued_at 必须严格晚于它，
    /// 否则遥控失联后不得复活（stale_after_override）。
    rc_takeover_start: i64,
}

impl Default for Inner {
    fn default() -> Self {
        Inner {
            clock_mode: ClockMode::Sim,
            sim_now: 0,
            estop: EstopState::default(),
            autonomous: SourceState::default(),
            remote: SourceState::default(),
            freshness_gate: 0,
            rc_takeover_start: 0,
        }
    }
}

pub struct Arbiter {
    inner: Mutex<Inner>,
    store: Mutex<Store>,
    limits: Limits,
}

/// 写入类请求的结果。
#[derive(Debug)]
pub struct IngestOutcome {
    pub decision_id: i64,
    pub accepted: bool,
    /// 消息是否真正改变了锁存/门控/存储（被抑制的速度命令仍会更新存储与序号）。
    pub effective: bool,
    pub decision: Decision,
}

/// 命令的“失效”原因（与实时优先级无关）：锁存、释放、TTL、租约、急停保鲜门控。
fn command_ineligibility(c: &StoredCommand, at: i64, freshness_gate: i64, estop_latched: bool) -> Option<String> {
    if estop_latched {
        return Some(ESTOP_LATCHED.to_string());
    }
    if c.revoked {
        return Some(LEASE_RELEASED.to_string());
    }
    if at >= c.ttl_expires_at {
        return Some(TTL_EXPIRED.to_string());
    }
    if at >= c.lease_expires_at {
        return Some(LEASE_EXPIRED.to_string());
    }
    if c.issued_at <= freshness_gate {
        return Some(STALE_AFTER_ESTOP.to_string());
    }
    None
}

impl Inner {
    fn source_mut(&mut self, s: Source) -> &mut SourceState {
        match s {
            Source::Autonomous => &mut self.autonomous,
            Source::Remote => &mut self.remote,
        }
    }

    fn source(&self, s: Source) -> &SourceState {
        match s {
            Source::Autonomous => &self.autonomous,
            Source::Remote => &self.remote,
        }
    }

    /// 纯投影：给定时刻的仲裁结果。不修改任何状态。
    fn evaluate(&self, at: i64) -> Decision {
        let mut suppressed = Vec::new();

        if self.estop.latched {
            for s in [Source::Remote, Source::Autonomous] {
                if let Some(c) = &self.source(s).command {
                    suppressed.push(make_suppressed(c, ESTOP_LATCHED));
                }
            }
            return stop(at, true, Some(ESTOP_LATCHED), suppressed);
        }

        // 计算各源“最后一条命令”在 at 时刻是否可用（不考虑相互优先级）。
        let rc_state = self.remote.command.as_ref().map(|c| {
            (
                c,
                command_ineligibility(c, at, self.freshness_gate, false),
            )
        });
        let au_state = self.autonomous.command.as_ref().map(|c| {
            let base = command_ineligibility(c, at, self.freshness_gate, false);
            // 自主命令额外受“遥控接管起点”门控，但该原因取决于 RC 是否 active。
            (c, base)
        });

        let rc_active = matches!(rc_state, Some((_, None)));

        // 自主的落选原因：
        // 1) 基础失效（TTL/租约/释放/急停保鲜门控）优先；
        // 2) 否则若 issued_at <= rc_takeover_start → stale_after_override（永不复活）；
        // 3) 否则 RC active → priority_override；
        // 4) 否则它就是可用的。
        let au_drop_reason = |c: &StoredCommand, base: Option<String>| -> Option<String> {
            base.or_else(|| {
                if c.issued_at <= self.rc_takeover_start {
                    Some(STALE_AFTER_OVERRIDE.to_string())
                } else if rc_active {
                    Some(PRIORITY_OVERRIDE.to_string())
                } else {
                    None
                }
            })
        };

        match rc_state {
            Some((rc, None)) => {
                if let Some((au, au_base)) = au_state {
                    if let Some(why) = au_drop_reason(au, au_base) {
                        suppressed.push(make_suppressed(au, &why));
                    }
                }
                select(at, rc, suppressed)
            }
            Some((rc, Some(why))) => {
                suppressed.push(make_suppressed(rc, &why));
                match au_state {
                    Some((au, au_base)) => match au_drop_reason(au, au_base) {
                        None => select(at, au, suppressed),
                        Some(w) => {
                            suppressed.push(make_suppressed(au, &w));
                            stop(at, false, Some(NO_ELIGIBLE), suppressed)
                        }
                    },
                    None => stop(at, false, Some(NO_ELIGIBLE), suppressed),
                }
            }
            None => match au_state {
                Some((au, au_base)) => match au_drop_reason(au, au_base) {
                    None => select(at, au, suppressed),
                    Some(why) => {
                        suppressed.push(make_suppressed(au, &why));
                        stop(at, false, Some(NO_ELIGIBLE), suppressed)
                    }
                },
                None => stop(at, false, Some(NO_ELIGIBLE), suppressed),
            },
        }
    }
}

fn make_suppressed(c: &StoredCommand, reason: &str) -> SuppressedCommand {
    SuppressedCommand {
        source: c.source.as_str().to_string(),
        seq: c.seq,
        lease_id: c.lease_id.clone(),
        reason: reason.to_string(),
    }
}

fn stop(at: i64, latched: bool, stop_reason: Option<&str>, suppressed: Vec<SuppressedCommand>) -> Decision {
    Decision {
        at,
        estop_latched: latched,
        selected: None,
        output: Velocity { vx: 0.0, wz: 0.0 },
        stop_reason: stop_reason.map(|s| s.to_string()),
        suppressed,
    }
}

fn select(at: i64, c: &StoredCommand, suppressed: Vec<SuppressedCommand>) -> Decision {
    Decision {
        at,
        estop_latched: false,
        selected: Some(SelectedCommand {
            source: c.source.as_str().to_string(),
            seq: c.seq,
            lease_id: c.lease_id.clone(),
            vx: c.vx,
            wz: c.wz,
        }),
        output: Velocity { vx: c.vx, wz: c.wz },
        stop_reason: None,
        suppressed,
    }
}

impl Arbiter {
    pub fn new(mut store: Store, clock_mode: ClockMode, initial_sim_now: i64, limits: Limits) -> rusqlite::Result<Self> {
        store.ensure_initial(clock_mode, initial_sim_now)?;
        let p = store.hydrate()?;
        // DB 中记录的时钟模式必须与启动参数一致（同一份数据不能两种模式混用）。
        assert_eq!(p.clock_mode.as_str(), clock_mode.as_str(), "clock_mode mismatch with existing database");
        let inner = Inner {
            clock_mode,
            sim_now: p.sim_now,
            estop: p.estop,
            autonomous: p.autonomous,
            remote: p.remote,
            freshness_gate: p.freshness_gate,
            rc_takeover_start: p.rc_takeover_start,
        };
        Ok(Arbiter {
            inner: Mutex::new(inner),
            store: Mutex::new(store),
            limits,
        })
    }

    pub fn clock_mode(&self) -> ClockMode {
        self.inner.lock().unwrap().clock_mode
    }

    pub fn sim_now(&self) -> i64 {
        self.inner.lock().unwrap().sim_now
    }

    /// 处理一条速度命令。
    pub fn ingest_command(
        &self,
        source: Source,
        req: CommandRequest,
        sim_at: Option<i64>,
        wall_now: Option<i64>,
    ) -> Result<IngestOutcome, (u16, ApiError)> {
        self.do_ingest(source, req, sim_at, wall_now)
    }

    fn validate_command(&self, req: &CommandRequest, at: i64) -> Result<(), (u16, String, Option<String>)> {
        if req.seq < 1 {
            return Err((422, VALIDATION.into(), Some("seq must be >= 1".into())));
        }
        if req.lease_id.is_empty() || req.lease_id.len() > 128 {
            return Err((422, VALIDATION.into(), Some("lease_id must be 1..=128 chars".into())));
        }
        if !req.vx.is_finite() || !req.wz.is_finite() {
            return Err((422, VALIDATION.into(), Some("vx/wz must be finite".into())));
        }
        if req.vx.abs() > self.limits.max_vx || req.wz.abs() > self.limits.max_wz {
            return Err((
                422,
                OUT_OF_RANGE.into(),
                Some(format!(
                    "|vx| <= {} and |wz| <= {} required",
                    self.limits.max_vx, self.limits.max_wz
                )),
            ));
        }
        if req.lease_ms < 1 || req.ttl_ms < 1 {
            return Err((422, VALIDATION.into(), Some("lease_ms/ttl_ms must be >= 1".into())));
        }
        if req.issued_at > at + self.limits.future_skew_ms {
            return Err((
                422,
                FUTURE_SKEW.into(),
                Some(format!(
                    "issued_at {} is more than {} ms ahead of {}",
                    req.issued_at, self.limits.future_skew_ms, at
                )),
            ));
        }
        if req.issued_at + req.ttl_ms <= at {
            return Err((409, TTL_EXPIRED.into(), Some(format!(
                "ttl expired at {} (now {})", req.issued_at + req.ttl_ms, at
            ))));
        }
        if req.issued_at + req.lease_ms <= at {
            return Err((409, LEASE_EXPIRED.into(), Some(format!(
                "lease expired at {} (now {})", req.issued_at + req.lease_ms, at
            ))));
        }
        Ok(())
    }

    fn do_ingest(
        &self,
        source: Source,
        req: CommandRequest,
        sim_at: Option<i64>,
        wall_now: Option<i64>,
    ) -> Result<IngestOutcome, (u16, ApiError)> {
        let mut inner = self.inner.lock().unwrap();

        let at = clock_at(&inner, sim_at, wall_now)?;

        let reject = |code: u16, reason: String, detail: Option<String>| {
            self.persist_reject(at, "command", Some(source.as_str()), Some(req.seq), &reason, detail.as_deref());
            Err((code, ApiError {
                error: http_error_name(code).to_string(),
                reason,
                detail,
            }))
        };

        if let Err((code, reason, detail)) = self.validate_command(&req, at) {
            return reject(code, reason, detail);
        }

        let last_seq = inner.source(source).last_seq;
        if req.seq <= last_seq {
            return reject(
                409,
                STALE_SEQ.into(),
                Some(format!("seq {} <= last accepted seq {}", req.seq, last_seq)),
            );
        }

        let stored = StoredCommand {
            source,
            seq: req.seq,
            lease_id: req.lease_id.clone(),
            vx: req.vx,
            wz: req.wz,
            issued_at: req.issued_at,
            lease_ms: req.lease_ms,
            ttl_ms: req.ttl_ms,
            lease_expires_at: req.issued_at + req.lease_ms,
            ttl_expires_at: req.issued_at + req.ttl_ms,
            accepted_at: at,
            revoked: false,
        };

        // 有效性判定（是否“生效”）：
        //  - 急停锁存：接受存储但不生效（投影中 estop_latched）
        //  - 保鲜门控失败（issued_at 不晚于急停解除时刻）：接受但不生效
        //  - 自主命令 issued_at 不晚于遥控接管起点：接受但不生效（旧命令永不复活）
        //  - 其余：生效
        let effective = !inner.estop.latched
            && req.issued_at > inner.freshness_gate
            && !(source == Source::Autonomous && req.issued_at <= inner.rc_takeover_start);

        // 更新内存状态
        let st = inner.source_mut(source);
        st.last_seq = req.seq;
        st.command = Some(stored.clone());

        if !inner.estop.latched && source == Source::Remote {
            // 遥控接管起点锚定“服务器观察到接管的时刻”（到达时 at），
            // 不信任来源自报 issued_at（其时钟可能偏差）：物理上在接管之前
            // 生成的自主命令一律视为旧命令，遥控失联后不得复活。取最大保持单调。
            if at > inner.rc_takeover_start {
                inner.rc_takeover_start = at;
            }
        }
        if inner.clock_mode == ClockMode::Sim {
            inner.sim_now = at;
        }

        let decision = inner.evaluate(at);
        let decision_json = serde_json::to_string(&decision).unwrap();

        let mut store = self.store.lock().unwrap();
        let conn = store.conn_mut();
        let tx = conn.transaction().map_err(internal)?;
        if inner.clock_mode == ClockMode::Sim {
            Store::update_meta(&tx, at).map_err(internal)?;
        }
        Store::update_source(&tx, source, inner.source(source)).map_err(internal)?;
        Store::update_gates(&tx, inner.freshness_gate, inner.rc_takeover_start).map_err(internal)?;
        let id = Store::insert_decision(
            &tx,
            at,
            source.as_str(),
            true,
            Some(source.as_str()),
            Some(req.seq),
            Some(effective),
            &decision_json,
        )
        .map_err(internal)?;
        tx.commit().map_err(internal)?;
        drop(store);

        Ok(IngestOutcome {
            decision_id: id,
            accepted: true,
            effective,
            decision,
        })
    }

    /// 处理急停事件（latch / release）。
    pub fn ingest_estop(
        &self,
        req: EstopRequest,
        sim_at: Option<i64>,
        wall_now: Option<i64>,
    ) -> Result<IngestOutcome, (u16, ApiError)> {
        let mut inner = self.inner.lock().unwrap();
        let at = clock_at(&inner, sim_at, wall_now)?;

        let reject = |code: u16, reason: &'static str, detail: Option<String>| {
            self.persist_reject(at, "estop", None, Some(req.seq), reason, detail.as_deref());
            Err((code, ApiError {
                error: http_error_name(code).to_string(),
                reason: reason.into(),
                detail,
            }))
        };

        if req.seq < 1 {
            return reject(422, VALIDATION, Some("seq must be >= 1".into()));
        }
        if req.issued_at > at + self.limits.future_skew_ms {
            return reject(422, FUTURE_SKEW, None);
        }
        if req.seq <= inner.estop.seq {
            return reject(
                409,
                STALE_SEQ,
                Some(format!("seq {} <= last estop seq {}", req.seq, inner.estop.seq)),
            );
        }

        let mut effective = false;
        match req.event {
            EstopEvent::Latch => {
                // 锁存幂等：重复 latch 不改变状态（但 seq 更新、留痕）。
                if !inner.estop.latched {
                    inner.estop.latched = true;
                    inner.estop.latched_at = Some(at);
                    effective = true;
                }
                inner.estop.seq = req.seq;
                inner.estop.issued_at = Some(req.issued_at);
                inner.estop.reason = req.reason.clone();
            }
            EstopEvent::Release => {
                if !inner.estop.latched {
                    return reject(409, NOT_LATCHED, Some("cannot release: estop is not latched".into()));
                }
                inner.estop.latched = false;
                inner.estop.seq = req.seq;
                inner.estop.issued_at = Some(req.issued_at);
                inner.estop.latched_at = None;
                inner.estop.reason = req.reason.clone();
                // 保鲜门控推进到解除时刻：只有 issued_at 严格晚于它的新命令可被选中。
                // 遥控接管起点同样推进，避免把锁存期间/之前的旧 RC 当作接管基准。
                if at > inner.freshness_gate {
                    inner.freshness_gate = at;
                }
                if at > inner.rc_takeover_start {
                    inner.rc_takeover_start = at;
                }
                effective = true;
            }
        }
        if inner.clock_mode == ClockMode::Sim {
            inner.sim_now = at;
        }

        let decision = inner.evaluate(at);
        let decision_json = serde_json::to_string(&decision).unwrap();

        let mut store = self.store.lock().unwrap();
        let conn = store.conn_mut();
        let tx = conn.transaction().map_err(internal)?;
        if inner.clock_mode == ClockMode::Sim {
            Store::update_meta(&tx, at).map_err(internal)?;
        }
        Store::update_estop(&tx, &inner.estop).map_err(internal)?;
        Store::update_gates(&tx, inner.freshness_gate, inner.rc_takeover_start).map_err(internal)?;
        let id = Store::insert_decision(
            &tx,
            at,
            "estop",
            true,
            Some("estop"),
            Some(req.seq),
            Some(effective),
            &decision_json,
        )
        .map_err(internal)?;
        tx.commit().map_err(internal)?;

        Ok(IngestOutcome {
            decision_id: id,
            accepted: true,
            effective,
            decision,
        })
    }

    /// 显式释放来源租约。
    pub fn release_lease(
        &self,
        source: Source,
        req: ReleaseRequest,
        sim_at: Option<i64>,
        wall_now: Option<i64>,
    ) -> Result<IngestOutcome, (u16, ApiError)> {
        let mut inner = self.inner.lock().unwrap();
        let at = clock_at(&inner, sim_at, wall_now)?;

        let reject = |code: u16, reason: &'static str, detail: Option<String>| {
            self.persist_reject(at, "release", Some(source.as_str()), Some(req.seq), reason, detail.as_deref());
            Err((code, ApiError {
                error: http_error_name(code).to_string(),
                reason: reason.into(),
                detail,
            }))
        };

        if req.seq < 1 {
            return reject(422, VALIDATION, Some("seq must be >= 1".into()));
        }
        if req.lease_id.is_empty() {
            return reject(422, VALIDATION, Some("lease_id required".into()));
        }
        if req.issued_at > at + self.limits.future_skew_ms {
            return reject(422, FUTURE_SKEW, None);
        }
        let last_seq = inner.source(source).last_seq;
        if req.seq <= last_seq {
            return reject(
                409,
                STALE_SEQ,
                Some(format!("seq {} <= last accepted seq {}", req.seq, last_seq)),
            );
        }

        // 找到当前存储命令；lease_id 不匹配则拒绝（不能释放别人的租约）。
        let cmd = match &inner.source(source).command {
            Some(c) => c.clone(),
            None => return reject(409, LEASE_MISMATCH, Some("no command stored for source".into())),
        };
        if cmd.lease_id != req.lease_id {
            return reject(
                409,
                LEASE_MISMATCH,
                Some(format!("stored lease_id is {}, not {}", cmd.lease_id, req.lease_id)),
            );
        }

        let mut effective = false;
        if !cmd.revoked {
            if let Some(c) = &mut inner.source_mut(source).command {
                c.revoked = true;
            }
            effective = true;
            // 遥控显式释放租约：接管终止于释放时刻，此前发出的旧自主命令仍过旧，
            // 不能恢复；必须等 issued_at 严格晚于释放时刻的新鲜自主命令。
            if source == Source::Remote && at > inner.rc_takeover_start {
                inner.rc_takeover_start = at;
            }
        }
        inner.source_mut(source).last_seq = req.seq;
        if inner.clock_mode == ClockMode::Sim {
            inner.sim_now = at;
        }

        let decision = inner.evaluate(at);
        let decision_json = serde_json::to_string(&decision).unwrap();

        let mut store = self.store.lock().unwrap();
        let conn = store.conn_mut();
        let tx = conn.transaction().map_err(internal)?;
        if inner.clock_mode == ClockMode::Sim {
            Store::update_meta(&tx, at).map_err(internal)?;
        }
        Store::update_source(&tx, source, inner.source(source)).map_err(internal)?;
        Store::update_gates(&tx, inner.freshness_gate, inner.rc_takeover_start).map_err(internal)?;
        let id = Store::insert_decision(
            &tx,
            at,
            "release",
            true,
            Some(source.as_str()),
            Some(req.seq),
            Some(effective),
            &decision_json,
        )
        .map_err(internal)?;
        tx.commit().map_err(internal)?;

        Ok(IngestOutcome {
            decision_id: id,
            accepted: true,
            effective,
            decision,
        })
    }

    /// 显式评估（POST /v1/decisions/evaluate）：推进 sim 时钟并产生一条决策记录。
    /// 不刷新租约（租约只由带新序号的新命令续期）。
    pub fn tick(&self, sim_at: Option<i64>, wall_now: Option<i64>) -> Result<IngestOutcome, (u16, ApiError)> {
        let mut inner = self.inner.lock().unwrap();
        let at = clock_at(&inner, sim_at, wall_now)?;

        if inner.clock_mode == ClockMode::Sim {
            inner.sim_now = at;
        }
        let decision = inner.evaluate(at);
        let decision_json = serde_json::to_string(&decision).unwrap();

        let mut store = self.store.lock().unwrap();
        let conn = store.conn_mut();
        let tx = conn.transaction().map_err(internal)?;
        if inner.clock_mode == ClockMode::Sim {
            Store::update_meta(&tx, at).map_err(internal)?;
        }
        let id = Store::insert_decision(
            &tx,
            at,
            "evaluate",
            true,
            None,
            None,
            Some(true),
            &decision_json,
        )
        .map_err(internal)?;
        tx.commit().map_err(internal)?;

        Ok(IngestOutcome {
            decision_id: id,
            accepted: true,
            effective: true,
            decision,
        })
    }

    /// GET 投影：绝不写库、绝不改状态、绝不刷新租约。
    /// sim 模式 at 缺省取最近 sim_now；wall 模式由 handler 注入系统时间。
    pub fn snapshot(&self, at: Option<i64>) -> Decision {
        let inner = self.inner.lock().unwrap();
        let at = at.unwrap_or_else(|| match inner.clock_mode {
            ClockMode::Sim => inner.sim_now,
            ClockMode::Wall => 0, // handler 在 wall 模式必须注入
        });
        inner.evaluate(at)
    }

    pub fn debug_state(&self) -> serde_json::Value {
        let inner = self.inner.lock().unwrap();
        serde_json::json!({
            "clock_mode": inner.clock_mode,
            "sim_now": inner.sim_now,
            "estop": inner.estop,
            "autonomous": inner.autonomous,
            "remote": inner.remote,
            "freshness_gate": inner.freshness_gate,
            "rc_takeover_start": inner.rc_takeover_start,
        })
    }

    /// 读取决策历史（只读，不刷新租约）。
    pub fn list_decisions(&self, limit: i64) -> Vec<serde_json::Value> {
        let store = self.store.lock().unwrap();
        let mut stmt = store
            .conn()
            .prepare(
                "SELECT id, created_at, kind, accepted, source, trigger_seq, effective, decision_json
                 FROM decisions ORDER BY id DESC LIMIT ?1",
            )
            .unwrap();
        let rows = stmt
            .query_map(params![limit.clamp(1, 500)], |row| {
                let json: String = row.get(7)?;
                let mut v = serde_json::json!({
                    "id": row.get::<_, i64>(0)?,
                    "created_at": row.get::<_, i64>(1)?,
                    "kind": row.get::<_, String>(2)?,
                    "accepted": row.get::<_, i64>(3)? != 0,
                    "source": row.get::<_, Option<String>>(4)?,
                    "trigger_seq": row.get::<_, Option<i64>>(5)?,
                    "effective": row.get::<_, Option<i64>>(6)?.map(|x| x != 0),
                });
                if let Some(obj) = v.as_object_mut() {
                    let d: serde_json::Value = serde_json::from_str(&json).unwrap();
                    obj.insert("decision".to_string(), d);
                }
                Ok(v)
            })
            .unwrap();
        rows.filter_map(|r| r.ok()).collect()
    }

    pub fn list_ingest_events(&self, limit: i64) -> Vec<serde_json::Value> {
        let store = self.store.lock().unwrap();
        let mut stmt = store
            .conn()
            .prepare(
                "SELECT id, at, kind, source, seq, accepted, reason, detail
                 FROM ingest_events ORDER BY id DESC LIMIT ?1",
            )
            .unwrap();
        let rows = stmt
            .query_map(params![limit.clamp(1, 500)], |row| {
                Ok(serde_json::json!({
                    "id": row.get::<_, i64>(0)?,
                    "at": row.get::<_, i64>(1)?,
                    "kind": row.get::<_, String>(2)?,
                    "source": row.get::<_, Option<String>>(3)?,
                    "seq": row.get::<_, Option<i64>>(4)?,
                    "accepted": row.get::<_, i64>(5)? != 0,
                    "reason": row.get::<_, String>(6)?,
                    "detail": row.get::<_, Option<String>>(7)?,
                }))
            })
            .unwrap();
        rows.filter_map(|r| r.ok()).collect()
    }

    /// 测试管理接口：清空并重置全部状态（时钟模式保持不变）。
    pub fn admin_reset(&self, initial_sim_now: i64) {
        let mode = {
            let mut inner = self.inner.lock().unwrap();
            let mode = inner.clock_mode;
            *inner = Inner::default();
            inner.clock_mode = mode;
            inner.sim_now = initial_sim_now;
            mode
        };

        let mut store = self.store.lock().unwrap();
        let conn = store.conn_mut();
        conn.execute_batch(
            "DELETE FROM decisions;
             DELETE FROM ingest_events;
             DELETE FROM meta;
             DELETE FROM estop_state;
             DELETE FROM source_state;
             DELETE FROM gates;",
        )
        .unwrap();
        store.ensure_initial(mode, initial_sim_now).unwrap();
    }

    fn persist_reject(
        &self,
        at: i64,
        kind: &str,
        source: Option<&str>,
        seq: Option<i64>,
        reason: &str,
        detail: Option<&str>,
    ) {
        let store = self.store.lock().unwrap();
        store
            .conn()
            .execute(
                "INSERT INTO ingest_events(at, kind, source, seq, accepted, reason, detail)
                 VALUES (?1,?2,?3,?4,0,?5,?6)",
                params![at, kind, source, seq, reason, detail],
            )
            .ok();
    }
}

fn clock_at(inner: &Inner, sim_at: Option<i64>, wall_now: Option<i64>) -> Result<i64, (u16, ApiError)> {
    match inner.clock_mode {
        ClockMode::Sim => {
            let at = sim_at.ok_or((400, ApiError::new("bad_request", "sim_clock_header_required")))?;
            if at < inner.sim_now {
                return Err((409, ApiError::new("conflict", CLOCK_REGRESSION)));
            }
            Ok(at)
        }
        ClockMode::Wall => {
            if sim_at.is_some() {
                return Err((400, ApiError::new("bad_request", "sim_clock_header_rejected_in_wall_mode")));
            }
            wall_now.ok_or((500, ApiError::new("internal", "wall_now_missing")))
        }
    }
}

fn http_error_name(code: u16) -> &'static str {
    match code {
        400 => "bad_request",
        401 => "unauthorized",
        404 => "not_found",
        409 => "conflict",
        422 => "unprocessable_entity",
        _ => "error",
    }
}

fn internal(e: rusqlite::Error) -> (u16, ApiError) {
    (500, ApiError::with_detail("internal", "database_error", e.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mk(seq: i64, issued: i64, lease: i64, ttl: i64) -> CommandRequest {
        CommandRequest {
            seq,
            lease_id: format!("l{seq}"),
            vx: 1.0,
            wz: 0.0,
            issued_at: issued,
            lease_ms: lease,
            ttl_ms: ttl,
        }
    }

    fn arb() -> Arbiter {
        let store = Store::open_in_memory().unwrap();
        Arbiter::new(store, ClockMode::Sim, 1000, Limits::default()).unwrap()
    }

    #[test]
    fn basic_priority_and_expiry() {
        let a = arb();
        // t=1000: autonomous 命令（issued 990，租约到 10000）
        a.ingest_command(Source::Autonomous, mk(1, 990, 9010, 20000), Some(1000), None)
            .unwrap();
        assert_eq!(a.snapshot(Some(1000)).selected.as_ref().unwrap().source, "autonomous");

        // t=2000: 遥控接管（issued 1990，租约到 5000）；旧自主命令 stale，永不复活
        a.ingest_command(Source::Remote, mk(1, 1990, 3010, 20000), Some(2000), None)
            .unwrap();
        let d = a.snapshot(Some(2000));
        assert_eq!(d.selected.as_ref().unwrap().source, "remote");
        assert_eq!(d.suppressed[0].reason, STALE_AFTER_OVERRIDE);

        // t=5001: 遥控租约到期，旧自主命令不得恢复 —— 输出零速
        let d = a.snapshot(Some(5001));
        assert!(d.selected.is_none());
        assert_eq!(d.stop_reason.as_deref(), Some(NO_ELIGIBLE));
        assert!(d
            .suppressed
            .iter()
            .any(|s| s.source == "remote" && s.reason == LEASE_EXPIRED));
        assert!(d
            .suppressed
            .iter()
            .any(|s| s.source == "autonomous" && s.reason == STALE_AFTER_OVERRIDE));

        // t=5001 收到新鲜自主命令（issued 5001）才恢复
        a.ingest_command(Source::Autonomous, mk(2, 5001, 1000, 20000), Some(5001), None)
            .unwrap();
        let d = a.snapshot(Some(5001));
        assert_eq!(d.selected.as_ref().unwrap().source, "autonomous");
        assert_eq!(d.selected.as_ref().unwrap().seq, 2);
    }

    #[test]
    fn priority_override_when_fresh_autonomy_exists() {
        // 自主命令 issued_at 晚于 RC 接管起点时：RC 在线被实时压制（priority_override），
        // RC 失联后这条“新鲜自主”可以接手，不需要新命令。
        let a = arb();
        // RC: issued 2000，租约到 5000
        a.ingest_command(Source::Remote, mk(1, 2000, 3000, 20000), Some(2000), None)
            .unwrap();
        // 自主: issued 3000（> 接管起点 2000），租约到 9000 —— 到达时被 RC 压制
        a.ingest_command(Source::Autonomous, mk(1, 3000, 6000, 20000), Some(3000), None)
            .unwrap();
        let d = a.snapshot(Some(3000));
        assert_eq!(d.selected.as_ref().unwrap().source, "remote");
        assert_eq!(d.suppressed[0].reason, PRIORITY_OVERRIDE);

        // RC 租约到期（5000）：新鲜自主自动接手
        let d = a.snapshot(Some(5001));
        assert_eq!(d.selected.as_ref().unwrap().source, "autonomous");
        assert!(d
            .suppressed
            .iter()
            .any(|s| s.source == "remote" && s.reason == LEASE_EXPIRED));

        // 自主租约也到期（9000）后输出零速
        let d = a.snapshot(Some(9001));
        assert!(d.selected.is_none());
        assert!(d
            .suppressed
            .iter()
            .any(|s| s.source == "autonomous" && s.reason == LEASE_EXPIRED));
    }

    #[test]
    fn stale_seq_and_expired_on_arrival_rejected() {
        let a = arb();
        a.ingest_command(Source::Remote, mk(5, 1000, 1000, 2000), Some(1000), None)
            .unwrap();
        let err = a
            .ingest_command(Source::Remote, mk(5, 1000, 1000, 2000), Some(1001), None)
            .unwrap_err();
        assert_eq!(err.0, 409);
        assert_eq!(err.1.reason, STALE_SEQ);
        let err = a
            .ingest_command(Source::Remote, mk(4, 1000, 1000, 2000), Some(1002), None)
            .unwrap_err();
        assert_eq!(err.1.reason, STALE_SEQ);

        // 到达时 TTL 已过 / 租约已过
        let err = a
            .ingest_command(Source::Remote, mk(6, 1000, 5000, 10), Some(2000), None)
            .unwrap_err();
        assert_eq!(err.1.reason, TTL_EXPIRED);
        let err = a
            .ingest_command(Source::Remote, mk(7, 1000, 10, 5000), Some(2000), None)
            .unwrap_err();
        assert_eq!(err.1.reason, LEASE_EXPIRED);

        // 被拒消息不入状态：seq=6/7 不推进 last_seq，seq=6 可在有效期内重发
        a.ingest_command(Source::Remote, mk(6, 2000, 1000, 2000), Some(2000), None)
            .unwrap();

        // NaN/越界速度拒绝
        let mut bad = mk(8, 2000, 1000, 2000);
        bad.vx = f64::NAN;
        assert_eq!(a.ingest_command(Source::Remote, bad, Some(2001), None).unwrap_err().1.reason, VALIDATION);
        let mut bad = mk(8, 2000, 1000, 2000);
        bad.vx = 99.0;
        assert_eq!(a.ingest_command(Source::Remote, bad, Some(2001), None).unwrap_err().1.reason, OUT_OF_RANGE);
    }

    #[test]
    fn estop_latches_and_gates_freshness_on_release() {
        let a = arb();
        a.ingest_command(Source::Autonomous, mk(1, 990, 9000, 20000), Some(1000), None)
            .unwrap();
        a.ingest_estop(
            EstopRequest {
                seq: 1,
                event: EstopEvent::Latch,
                issued_at: 1500,
                reason: Some("test".into()),
            },
            Some(1500),
            None,
        )
        .unwrap();
        let d = a.snapshot(Some(1500));
        assert!(d.estop_latched);
        assert!(d.selected.is_none());
        assert_eq!(d.stop_reason.as_deref(), Some(ESTOP_LATCHED));
        assert!(d.suppressed.iter().all(|s| s.reason == ESTOP_LATCHED));

        // 锁存期间到达的 RC：被接受存储但抑制
        a.ingest_command(Source::Remote, mk(1, 1600, 5000, 20000), Some(1600), None)
            .unwrap();
        assert_eq!(a.snapshot(Some(1600)).suppressed.len(), 2);

        // 再来 latch（更高 seq）：幂等，effective=false
        let out = a
            .ingest_estop(
                EstopRequest { seq: 2, event: EstopEvent::Latch, issued_at: 1700, reason: None },
                Some(1700),
                None,
            )
            .unwrap();
        assert!(!out.effective);

        // t=3000 解除
        a.ingest_estop(
            EstopRequest { seq: 3, event: EstopEvent::Release, issued_at: 3000, reason: None },
            Some(3000),
            None,
        )
        .unwrap();

        // 解除后：锁存前的自主、锁存期间的 RC 都不新鲜
        let d = a.snapshot(Some(3001));
        assert!(!d.estop_latched);
        assert!(d.selected.is_none());
        let reasons: Vec<_> = d.suppressed.iter().map(|s| (&s.source[..], &s.reason[..])).collect();
        assert!(reasons.contains(&("remote", STALE_AFTER_ESTOP)));
        assert!(reasons.contains(&("autonomous", STALE_AFTER_ESTOP)));

        // issued_at == 3000（同刻）仍被门控挡住（必须严格晚于）
        a.ingest_command(Source::Remote, mk(2, 3000, 1000, 20000), Some(3002), None)
            .unwrap();
        let d = a.snapshot(Some(3002));
        assert!(d.selected.is_none());
        assert_eq!(d.suppressed[0].reason, STALE_AFTER_ESTOP);

        // issued_at=3001 的新鲜 RC 生效
        a.ingest_command(Source::Remote, mk(3, 3001, 1000, 20000), Some(3003), None)
            .unwrap();
        assert_eq!(a.snapshot(Some(3003)).selected.as_ref().unwrap().source, "remote");

        // 未锁存时 release 报错；旧 estop seq 重放报错
        let err = a
            .ingest_estop(
                EstopRequest { seq: 3, event: EstopEvent::Release, issued_at: 3099, reason: None },
                Some(3099),
                None,
            )
            .unwrap_err();
        assert_eq!(err.1.reason, STALE_SEQ);
        let err = a
            .ingest_estop(
                EstopRequest { seq: 4, event: EstopEvent::Release, issued_at: 3100, reason: None },
                Some(3100),
                None,
            )
            .unwrap_err();
        assert_eq!(err.1.reason, NOT_LATCHED);
    }

    #[test]
    fn explicit_release_does_not_revive_autonomy() {
        let a = arb();
        a.ingest_command(Source::Autonomous, mk(1, 1000, 20000, 30000), Some(1000), None)
            .unwrap();
        a.ingest_command(Source::Remote, mk(1, 2000, 10000, 30000), Some(2000), None)
            .unwrap();
        // RC 在 t=4000 显式释放
        a.release_lease(
            Source::Remote,
            ReleaseRequest { seq: 2, lease_id: "l1".into(), issued_at: 3999 },
            Some(4000),
            None,
        )
        .unwrap();
        let d = a.snapshot(Some(4000));
        assert!(d.selected.is_none());
        assert!(d
            .suppressed
            .iter()
            .any(|s| s.source == "remote" && s.reason == LEASE_RELEASED));
        assert!(d
            .suppressed
            .iter()
            .any(|s| s.source == "autonomous" && s.reason == STALE_AFTER_OVERRIDE));

        // 释放后到达的新鲜自主（issued > 4000）可以接手
        a.ingest_command(Source::Autonomous, mk(2, 4001, 1000, 20000), Some(4001), None)
            .unwrap();
        assert_eq!(a.snapshot(Some(4001)).selected.as_ref().unwrap().source, "autonomous");

        // lease_id 不匹配 / seq 回退
        let err = a
            .release_lease(
                Source::Autonomous,
                ReleaseRequest { seq: 3, lease_id: "wrong".into(), issued_at: 4002 },
                Some(4002),
                None,
            )
            .unwrap_err();
        assert_eq!(err.1.reason, LEASE_MISMATCH);
    }

    #[test]
    fn same_instant_remote_beats_autonomy() {
        // 同一 issued_at、同一到达时刻：RC 胜出，与到达顺序无关。
        for reverse in [false, true] {
            let a = arb();
            let send = |source: Source| {
                a.ingest_command(source, mk(1, 5000, 5000, 10000), Some(5000), None).unwrap()
            };
            if reverse {
                send(Source::Remote);
                send(Source::Autonomous);
            } else {
                send(Source::Autonomous);
                send(Source::Remote);
            }
            let d = a.snapshot(Some(5000));
            assert_eq!(d.selected.as_ref().unwrap().source, "remote");
            // 自主 issued_at=5000 <= 接管起点 5000：stale，RC 失联后不复活
            assert_eq!(d.suppressed[0].reason, STALE_AFTER_OVERRIDE);
        }

        // RC 失联、新鲜自主接手后，RC 同刻重新上线：自主被 priority_override（它足够新）
        let a = arb();
        a.ingest_command(Source::Remote, mk(1, 5000, 1000, 10000), Some(5000), None).unwrap();
        a.ingest_command(Source::Autonomous, mk(1, 6050, 5000, 10000), Some(6050), None).unwrap();
        // 6050 时 RC 已过期，自主直接选中
        assert_eq!(a.snapshot(Some(6050)).selected.as_ref().unwrap().source, "autonomous");
        // RC 重新上线 issued 7000；7100 自主发来新鲜命令（issued 7100 > 接管起点 7000）
        a.ingest_command(Source::Remote, mk(2, 7000, 2000, 10000), Some(7000), None).unwrap();
        a.ingest_command(Source::Autonomous, mk(2, 7100, 5000, 10000), Some(7100), None).unwrap();
        let d = a.snapshot(Some(7100));
        assert_eq!(d.selected.as_ref().unwrap().source, "remote");
        assert_eq!(d.suppressed[0].reason, PRIORITY_OVERRIDE);
        // RC 9000 失联：新鲜自主（issued 7100 > 接管起点 7000）自动接手，无需新命令
        let d = a.snapshot(Some(9001));
        assert_eq!(d.selected.as_ref().unwrap().source, "autonomous");
        assert_eq!(d.selected.as_ref().unwrap().seq, 2);
        assert!(d
            .suppressed
            .iter()
            .any(|s| s.source == "remote" && s.reason == LEASE_EXPIRED));
        // 而更早的旧自主 seq1（issued 6050）则永远不许复活
        let a2 = arb();
        a2.ingest_command(Source::Remote, mk(1, 7000, 2000, 10000), Some(7000), None).unwrap();
        // 只有旧自主 issued 6050（RC 接管之前），RC 失联后零速
        let mut old = mk(1, 6050, 5000, 10000);
        old.lease_id = "pre-rc".into();
        a2.ingest_command(Source::Autonomous, old, Some(7000), None).unwrap();
        let d = a2.snapshot(Some(9001));
        assert!(d.selected.is_none());
        assert!(d
            .suppressed
            .iter()
            .any(|s| s.source == "autonomous" && s.reason == STALE_AFTER_OVERRIDE));
    }

    #[test]
    fn queries_do_not_refresh_leases() {
        let a = arb();
        a.ingest_command(Source::Remote, mk(1, 1000, 1000, 5000), Some(1000), None)
            .unwrap();
        a.snapshot(Some(1500));
        a.tick(Some(1800), None).unwrap();
        let d = a.snapshot(Some(2001));
        assert!(d.selected.is_none());
        assert_eq!(
            d.suppressed.iter().find(|s| s.source == "remote").unwrap().reason,
            LEASE_EXPIRED
        );
    }

    #[test]
    fn clock_cannot_go_backwards() {
        let a = arb();
        a.tick(Some(2000), None).unwrap();
        let err = a.tick(Some(1999), None).unwrap_err();
        assert_eq!(err.1.reason, CLOCK_REGRESSION);
    }
}
