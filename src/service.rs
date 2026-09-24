use crate::clock::Clock;
use crate::error::AppError;
use crate::model::*;
use hmac::{Hmac, Mac};
use rand::RngCore;
use rusqlite::{params, Connection, OptionalExtension};
use sha2::Sha256;
use std::sync::{Arc, Mutex};

type HmacSha256 = Hmac<Sha256>;

pub struct WatchdogService {
    conn: Mutex<Connection>,
    clock: Arc<dyn Clock>,
}

/// True when firmware counter `a` is strictly newer than `b` under natural
/// 32-bit unsigned wrapping (RFC 1982 style, half-of-modulus rule).
/// Equal values are never "newer": a repeated heartbeat carries no progress.
pub fn wrapping_gt_u32(a: u32, b: u32) -> bool {
    let diff = a.wrapping_sub(b);
    diff != 0 && diff < (1u32 << 31)
}

fn validate_id(id: &str) -> Result<(), AppError> {
    if !(1..=64).contains(&id.len())
        || !id
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'.' || c == b'_' || c == b'-')
    {
        return Err(AppError::BadRequest(
            "id must be 1..=64 chars of [A-Za-z0-9._-]".into(),
        ));
    }
    Ok(())
}

fn deadline(start: i64, window: i64) -> i64 {
    start.saturating_add(window)
}

fn task_view(t: &TaskRow) -> TaskView {
    TaskView {
        task_id: t.task_id.clone(),
        last_counter: t.last_counter.map(|c| c as u32),
        advanced_this_window: t.advanced_this_window,
        last_report_ms: t.last_report_ms,
    }
}

fn audit(
    conn: &Connection,
    device_id: &str,
    at_ms: i64,
    event: &str,
    detail: &str,
) -> Result<(), AppError> {
    conn.execute(
        "INSERT INTO audit_log (device_id, at_ms, event, detail) VALUES (?1, ?2, ?3, ?4)",
        params![device_id, at_ms, event, detail],
    )?;
    Ok(())
}

impl WatchdogService {
    pub fn new(conn: Connection, clock: Arc<dyn Clock>) -> Self {
        Self {
            conn: Mutex::new(conn),
            clock,
        }
    }

    fn now(&self) -> i64 {
        self.clock.now_ms()
    }

    fn load_device(&self, conn: &Connection, id: &str) -> Result<DeviceRow, AppError> {
        conn.query_row(
            "SELECT device_id, status, fault_generation, consecutive_resets, window_ms,
                    reset_threshold, challenge_ttl_ms, window_started_ms, last_feed_ms,
                    last_reset_reason, last_reset_source, last_reset_at_ms,
                    last_progress_json, total_feeds, total_resets, operator_key
             FROM devices WHERE device_id = ?1",
            params![id],
            |r| {
                Ok(DeviceRow {
                    device_id: r.get(0)?,
                    status: r.get(1)?,
                    fault_generation: r.get(2)?,
                    consecutive_resets: r.get(3)?,
                    window_ms: r.get(4)?,
                    reset_threshold: r.get(5)?,
                    challenge_ttl_ms: r.get(6)?,
                    window_started_ms: r.get(7)?,
                    last_feed_ms: r.get(8)?,
                    last_reset_reason: r.get(9)?,
                    last_reset_source: r.get(10)?,
                    last_reset_at_ms: r.get(11)?,
                    last_progress_json: r.get(12)?,
                    total_feeds: r.get(13)?,
                    total_resets: r.get(14)?,
                    operator_key: r.get(15)?,
                })
            },
        )
        .optional()?
        .ok_or_else(|| AppError::NotFound(format!("device '{id}'")))
    }

    fn load_tasks(&self, conn: &Connection, device_id: &str) -> Result<Vec<TaskRow>, AppError> {
        let mut stmt = conn.prepare(
            "SELECT task_id, last_counter, last_report_ms, advanced_this_window
             FROM tasks WHERE device_id = ?1 ORDER BY task_id",
        )?;
        let rows = stmt.query_map(params![device_id], |r| {
            Ok(TaskRow {
                task_id: r.get(0)?,
                last_counter: r.get(1)?,
                last_report_ms: r.get(2)?,
                advanced_this_window: r.get::<_, i64>(3)? != 0,
            })
        })?;
        Ok(rows.collect::<rusqlite::Result<Vec<_>>>()?)
    }

    fn boot_count(&self, conn: &Connection, device_id: &str) -> Result<Option<i64>, AppError> {
        Ok(conn
            .query_row(
                "SELECT boot_count FROM boot_counts WHERE device_id = ?1",
                params![device_id],
                |r| r.get(0),
            )
            .optional()?)
    }

    // ---------------------------------------------------------------- devices

    pub fn create_device(&self, req: CreateDeviceReq) -> Result<StatusResp, AppError> {
        validate_id(&req.device_id)?;
        let window = req.window_ms.unwrap_or(DEFAULT_WINDOW_MS);
        let threshold = req.reset_threshold.unwrap_or(DEFAULT_RESET_THRESHOLD);
        let ttl = req.challenge_ttl_ms.unwrap_or(DEFAULT_CHALLENGE_TTL_MS);
        if !(50..=3_600_000).contains(&window) {
            return Err(AppError::BadRequest("window_ms must be in 50..=3600000".into()));
        }
        if !(1..=100).contains(&threshold) {
            return Err(AppError::BadRequest("reset_threshold must be in 1..=100".into()));
        }
        if !(1_000..=3_600_000).contains(&ttl) {
            return Err(AppError::BadRequest(
                "challenge_ttl_ms must be in 1000..=3600000".into(),
            ));
        }
        let key = req.operator_key.unwrap_or_else(|| DEFAULT_OPERATOR_KEY.into());
        if key.len() < 8 {
            return Err(AppError::BadRequest("operator_key too short (min 8 bytes)".into()));
        }

        let now = self.now();
        let conn = self.conn.lock().unwrap();
        let exists = conn
            .query_row(
                "SELECT 1 FROM devices WHERE device_id = ?1",
                params![req.device_id],
                |_| Ok(()),
            )
            .optional()?
            .is_some();
        if exists {
            return Err(AppError::Conflict(format!(
                "device '{}' already exists",
                req.device_id
            )));
        }
        conn.execute(
            "INSERT INTO devices
                (device_id, status, fault_generation, consecutive_resets, window_ms,
                 reset_threshold, challenge_ttl_ms, window_started_ms, last_feed_ms,
                 total_feeds, total_resets, created_ms, operator_key)
             VALUES (?1, ?2, 0, 0, ?3, ?4, ?5, ?6, NULL, 0, 0, ?6, ?7)",
            params![
                req.device_id,
                STATUS_RUNNING,
                window,
                threshold,
                ttl,
                now,
                key,
            ],
        )?;
        audit(&conn, &req.device_id, now, "device_created", "device registered")?;
        self.status_locked(&conn, &req.device_id)
    }

    pub fn register_task(
        &self,
        device_id: &str,
        req: RegisterTaskReq,
    ) -> Result<StatusResp, AppError> {
        validate_id(device_id)?;
        validate_id(&req.task_id)?;
        let now = self.now();
        let conn = self.conn.lock().unwrap();
        let _dev = self.load_device(&conn, device_id)?;
        // Seeding a baseline is NOT progress: advanced_this_window stays 0.
        conn.execute(
            "INSERT INTO tasks (device_id, task_id, last_counter, last_report_ms, advanced_this_window)
             VALUES (?1, ?2, ?3, ?4, 0)
             ON CONFLICT (device_id, task_id)
             DO UPDATE SET last_counter = excluded.last_counter,
                           last_report_ms = excluded.last_report_ms",
            params![
                device_id,
                req.task_id,
                req.initial_counter.map(|c| c as i64),
                req.initial_counter.map(|_| now),
            ],
        )?;
        audit(
            &conn,
            device_id,
            now,
            "task_registered",
            &format!(
                "task '{}' baseline={:?}",
                req.task_id, req.initial_counter
            ),
        )?;
        self.status_locked(&conn, device_id)
    }

    // ------------------------------------------------------------- heartbeat

    /// If the open window has expired, record one watchdog reset and re-arm.
    /// Backward-moving clocks (`now < start`) and short clocks (window not yet
    /// reached) never trigger a reset: elapsed time is plain subtraction.
    fn process_timeout_locked(
        &self,
        conn: &Connection,
        dev: &mut DeviceRow,
        now: i64,
    ) -> Result<bool, AppError> {
        if dev.status != STATUS_RUNNING {
            return Ok(false);
        }
        if now.saturating_sub(dev.window_started_ms) >= dev.window_ms {
            self.apply_reset_locked(
                conn,
                dev,
                now,
                "watchdog_timeout",
                "watchdog window expired without all tasks advancing",
                None,
                true,
            )?;
            Ok(true)
        } else {
            Ok(false)
        }
    }

    pub fn heartbeat(
        &self,
        device_id: &str,
        req: HeartbeatReq,
    ) -> Result<HeartbeatResp, AppError> {
        if req.reports.is_empty() {
            return Err(AppError::BadRequest("reports must not be empty".into()));
        }
        let mut seen = std::collections::HashSet::new();
        for r in &req.reports {
            if !seen.insert(r.task_id.clone()) {
                return Err(AppError::BadRequest(format!(
                    "duplicate report for task '{}'",
                    r.task_id
                )));
            }
        }

        let now = self.now();
        let conn = self.conn.lock().unwrap();
        let mut dev = self.load_device(&conn, device_id)?;
        let reset_applied = self.process_timeout_locked(&conn, &mut dev, now)?;

        // In safe mode the watchdog is deliberately not fed. Heartbeats are
        // accepted (and recorded as reports) but cannot leave safe mode.
        let mut tasks = self.load_tasks(&conn, device_id)?;
        if dev.status == STATUS_SAFE_MODE {
            for rep in &req.reports {
                if let Some(t) = tasks.iter_mut().find(|t| t.task_id == rep.task_id) {
                    t.last_report_ms = Some(now);
                    conn.execute(
                        "UPDATE tasks SET last_report_ms = ?3
                         WHERE device_id = ?1 AND task_id = ?2",
                        params![device_id, t.task_id, now],
                    )?;
                }
            }
            return Ok(HeartbeatResp {
                device_id: device_id.into(),
                fed: false,
                safe_mode: true,
                reason: "device in safe_mode: heartbeat acknowledged, watchdog NOT fed".into(),
                now_ms: now,
                window_started_ms: dev.window_started_ms,
                window_deadline_ms: deadline(dev.window_started_ms, dev.window_ms),
                consecutive_resets: dev.consecutive_resets,
                fault_generation: dev.fault_generation,
                tasks: tasks.iter().map(task_view).collect(),
            });
        }

        for rep in &req.reports {
            if !tasks.iter().any(|t| t.task_id == rep.task_id) {
                return Err(AppError::BadRequest(format!(
                    "unknown task '{}'; register it first",
                    rep.task_id
                )));
            }
        }

        for rep in &req.reports {
            let t = tasks
                .iter_mut()
                .find(|t| t.task_id == rep.task_id)
                .expect("validated above");
            let advanced = match t.last_counter {
                // First report after registration seeds the baseline and counts.
                None => true,
                Some(prev) => wrapping_gt_u32(rep.counter, prev as u32),
            };
            if advanced {
                t.last_counter = Some(rep.counter as i64);
                t.advanced_this_window = true;
            }
            t.last_report_ms = Some(now);
            conn.execute(
                "UPDATE tasks SET last_counter = ?3, last_report_ms = ?4,
                                 advanced_this_window = ?5
                 WHERE device_id = ?1 AND task_id = ?2",
                params![
                    device_id,
                    t.task_id,
                    t.last_counter,
                    now,
                    t.advanced_this_window as i64,
                ],
            )?;
        }

        // Feed only when EVERY registered task advanced; equal counters mean
        // "alive but stuck" and must not feed.
        let fed = !tasks.is_empty() && tasks.iter().all(|t| t.advanced_this_window);
        let reason = if fed {
            // Re-arm the window, clear the failure streak.
            let progress = serde_json::Map::from_iter(
                tasks
                    .iter()
                    .map(|t| (t.task_id.clone(), serde_json::json!(t.last_counter.map(|c| c as u32)))),
            );
            conn.execute(
                "UPDATE devices
                    SET window_started_ms = ?2, last_feed_ms = ?2,
                        consecutive_resets = 0, total_feeds = total_feeds + 1,
                        last_progress_json = ?3
                 WHERE device_id = ?1",
                params![device_id, now, serde_json::Value::Object(progress).to_string()],
            )?;
            conn.execute(
                "UPDATE tasks SET advanced_this_window = 0 WHERE device_id = ?1",
                params![device_id],
            )?;
            for t in tasks.iter_mut() {
                t.advanced_this_window = false;
            }
            dev.window_started_ms = now;
            dev.consecutive_resets = 0;
            "all tasks advanced within window: watchdog fed".to_string()
        } else if reset_applied {
            "watchdog timeout recorded this tick; window re-armed, waiting for full progress"
                .to_string()
        } else {
            "waiting: not every registered task has advanced since the window opened".to_string()
        };

        Ok(HeartbeatResp {
            device_id: device_id.into(),
            fed,
            safe_mode: false,
            reason,
            now_ms: now,
            window_started_ms: dev.window_started_ms,
            window_deadline_ms: deadline(dev.window_started_ms, dev.window_ms),
            consecutive_resets: dev.consecutive_resets,
            fault_generation: dev.fault_generation,
            tasks: tasks.iter().map(task_view).collect(),
        })
    }

    // --------------------------------------------------------------- resets

    /// Apply a reset: persist reason, bump streaks, re-arm, and enter safe
    /// mode once the threshold is reached (incrementing fault generation).
    #[allow(clippy::too_many_arguments)]
    fn apply_reset_locked(
        &self,
        conn: &Connection,
        dev: &mut DeviceRow,
        now: i64,
        source: &str,
        reason: &str,
        boot_count: Option<i64>,
        counted: bool,
    ) -> Result<(), AppError> {
        dev.total_resets += 1;
        if counted {
            dev.consecutive_resets += 1;
        }
        dev.last_reset_reason = Some(reason.to_string());
        dev.last_reset_source = Some(source.to_string());
        dev.last_reset_at_ms = Some(now);
        dev.window_started_ms = now; // re-arm the window after reset

        let mut entered_safe = false;
        if counted && dev.status == STATUS_RUNNING && dev.consecutive_resets >= dev.reset_threshold
        {
            dev.status = STATUS_SAFE_MODE.into();
            dev.fault_generation += 1;
            entered_safe = true;
        }

        conn.execute(
            "UPDATE devices
                SET status = ?2, fault_generation = ?3, consecutive_resets = ?4,
                    window_started_ms = ?5, last_reset_reason = ?6,
                    last_reset_source = ?7, last_reset_at_ms = ?8, total_resets = ?9
             WHERE device_id = ?1",
            params![
                dev.device_id,
                dev.status,
                dev.fault_generation,
                dev.consecutive_resets,
                dev.window_started_ms,
                dev.last_reset_reason,
                dev.last_reset_source,
                dev.last_reset_at_ms,
                dev.total_resets,
            ],
        )?;
        // No task is "partway through the window" after a reset.
        conn.execute(
            "UPDATE tasks SET advanced_this_window = 0 WHERE device_id = ?1",
            params![dev.device_id],
        )?;
        if let Some(bc) = boot_count {
            conn.execute(
                "INSERT INTO boot_counts (device_id, boot_count) VALUES (?1, ?2)
                 ON CONFLICT (device_id)
                 DO UPDATE SET boot_count = MAX(boot_counts.boot_count, excluded.boot_count)",
                params![dev.device_id, bc],
            )?;
        }
        let stored_boot = self.boot_count(conn, &dev.device_id)?;
        conn.execute(
            "INSERT INTO reset_records
                (device_id, seq, at_ms, source, reason, boot_count, counted,
                 consecutive_after, fault_generation_after)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            params![
                dev.device_id,
                dev.total_resets,
                now,
                source,
                reason,
                stored_boot,
                counted as i64,
                dev.consecutive_resets,
                dev.fault_generation,
            ],
        )?;

        audit(
            conn,
            &dev.device_id,
            now,
            "reset",
            &format!(
                "source={source} reason='{reason}' counted={counted} streak={}",
                dev.consecutive_resets
            ),
        )?;
        if entered_safe {
            // Old challenge material is invalid for the new fault generation.
            conn.execute(
                "DELETE FROM challenges WHERE device_id = ?1",
                params![dev.device_id],
            )?;
            audit(
                conn,
                &dev.device_id,
                now,
                "safe_mode_entered",
                &format!(
                    "{} consecutive resets reached threshold {}; fault generation -> {}",
                    dev.consecutive_resets, dev.reset_threshold, dev.fault_generation
                ),
            )?;
        }
        Ok(())
    }

    /// Firmware reports a reset observed across its own (re)boot.
    ///
    /// In running mode it is counted exactly like a watchdog timeout. In safe
    /// mode it is recorded for forensics but does NOT count toward the streak
    /// and cannot leave safe mode.
    pub fn report_reset(
        &self,
        device_id: &str,
        req: ReportResetReq,
    ) -> Result<StatusResp, AppError> {
        if req.reason.trim().is_empty() {
            return Err(AppError::BadRequest("reason must not be empty".into()));
        }
        let source = req.source.unwrap_or_else(|| "firmware_report".into());
        let now = self.now();
        let conn = self.conn.lock().unwrap();
        let mut dev = self.load_device(&conn, device_id)?;

        if dev.status == STATUS_SAFE_MODE {
            dev.total_resets += 1;
            conn.execute(
                "UPDATE devices SET total_resets = ?2 WHERE device_id = ?1",
                params![device_id, dev.total_resets],
            )?;
            if let Some(bc) = req.boot_count {
                conn.execute(
                    "INSERT INTO boot_counts (device_id, boot_count) VALUES (?1, ?2)
                     ON CONFLICT (device_id)
                     DO UPDATE SET boot_count = MAX(boot_counts.boot_count, excluded.boot_count)",
                    params![device_id, bc],
                )?;
            }
            let stored_boot = self.boot_count(&conn, device_id)?;
            conn.execute(
                "INSERT INTO reset_records
                    (device_id, seq, at_ms, source, reason, boot_count, counted,
                     consecutive_after, fault_generation_after)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, 0, ?7, ?8)",
                params![
                    device_id,
                    dev.total_resets,
                    now,
                    source,
                    req.reason,
                    stored_boot,
                    dev.consecutive_resets,
                    dev.fault_generation,
                ],
            )?;
            audit(
                &conn,
                device_id,
                now,
                "reset_ignored",
                &format!("reset '{source}' reported while in safe_mode; not counted"),
            )?;
            return self.status_locked(&conn, device_id);
        }

        self.apply_reset_locked(
            &conn,
            &mut dev,
            now,
            &source,
            &req.reason,
            req.boot_count,
            true,
        )?;
        self.status_locked(&conn, device_id)
    }

    pub fn tick(&self, device_id: &str) -> Result<TickResp, AppError> {
        let now = self.now();
        let conn = self.conn.lock().unwrap();
        let mut dev = self.load_device(&conn, device_id)?;
        let reset_applied = self.process_timeout_locked(&conn, &mut dev, now)?;
        Ok(TickResp {
            device_id: device_id.into(),
            now_ms: now,
            reset_applied,
            status: dev.status.clone(),
            consecutive_resets: dev.consecutive_resets,
            fault_generation: dev.fault_generation,
            window_started_ms: dev.window_started_ms,
            window_deadline_ms: deadline(dev.window_started_ms, dev.window_ms),
        })
    }

    // ------------------------------------------------------------- safe mode

    pub fn request_challenge(&self, device_id: &str) -> Result<ChallengeResp, AppError> {
        let now = self.now();
        let conn = self.conn.lock().unwrap();
        let dev = self.load_device(&conn, device_id)?;
        if dev.status != STATUS_SAFE_MODE {
            return Err(AppError::Conflict(
                "device is not in safe_mode; no challenge needed".into(),
            ));
        }
        let mut bytes = [0u8; 32];
        rand::rngs::OsRng.fill_bytes(&mut bytes);
        let nonce = hex::encode(bytes);
        let expires = now.saturating_add(dev.challenge_ttl_ms);
        conn.execute(
            "INSERT INTO challenges (device_id, generation, nonce, created_ms, expires_ms)
             VALUES (?1, ?2, ?3, ?4, ?5)
             ON CONFLICT (device_id)
             DO UPDATE SET generation = excluded.generation, nonce = excluded.nonce,
                           created_ms = excluded.created_ms, expires_ms = excluded.expires_ms",
            params![device_id, dev.fault_generation, nonce, now, expires],
        )?;
        audit(
            &conn,
            device_id,
            now,
            "challenge_issued",
            &format!("generation={} ttl_ms={}", dev.fault_generation, dev.challenge_ttl_ms),
        )?;
        Ok(ChallengeResp {
            device_id: device_id.into(),
            generation: dev.fault_generation,
            challenge: nonce,
            issued_at_ms: now,
            expires_at_ms: expires,
            sign_hint: "HMAC_SHA256(operator_key, \"{generation}:{challenge}\") as lowercase hex".into(),
        })
    }

    pub fn clear_safe_mode(&self, device_id: &str, req: ClearReq) -> Result<ClearResp, AppError> {
        let now = self.now();
        let conn = self.conn.lock().unwrap();
        let dev = self.load_device(&conn, device_id)?;

        let reject = |detail: &str| -> Result<ClearResp, AppError> {
            conn.execute(
                "INSERT INTO clearance_records (device_id, at_ms, generation, accepted, detail)
                 VALUES (?1, ?2, ?3, 0, ?4)",
                params![device_id, now, req.generation, detail],
            )?;
            audit(&conn, device_id, now, "clear_rejected", detail)?;
            Err(AppError::Forbidden(detail.to_string()))
        };

        if dev.status != STATUS_SAFE_MODE {
            let detail = "device is not in safe_mode";
            conn.execute(
                "INSERT INTO clearance_records (device_id, at_ms, generation, accepted, detail)
                 VALUES (?1, ?2, ?3, 0, ?4)",
                params![device_id, now, req.generation, detail],
            )?;
            return Err(AppError::Conflict(detail.into()));
        }
        // Stale request: operator signed for a different (older) fault.
        if req.generation != dev.fault_generation {
            return reject(&format!(
                "stale clearance request: signed generation {} != current fault generation {}",
                req.generation, dev.fault_generation
            ));
        }
        let row: Option<(String, i64, i64)> = conn
            .query_row(
                "SELECT nonce, generation, expires_ms FROM challenges WHERE device_id = ?1",
                params![device_id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()?;
        let (nonce, challenge_gen, expires) = match row {
            Some(v) => v,
            None => return reject("no outstanding challenge; request one first"),
        };
        if challenge_gen != dev.fault_generation {
            return reject("challenge belongs to a different fault generation");
        }
        if now > expires {
            return reject("challenge expired; request a new one");
        }
        if nonce != req.challenge {
            return reject("challenge nonce does not match the one issued");
        }

        // Real cryptographic verification: HMAC-SHA256, constant-time compare.
        let mut mac = <HmacSha256 as Mac>::new_from_slice(dev.operator_key.as_bytes())
            .expect("HMAC accepts keys of any length");
        let message = format!("{}:{}", req.generation, nonce);
        Mac::update(&mut mac, message.as_bytes());
        let expected = mac.finalize().into_bytes();
        let provided = match hex::decode(req.hmac_hex.trim()) {
            Ok(v) => v,
            Err(_) => return reject("hmac_hex is not valid hex"),
        };
        use subtle::ConstantTimeEq;
        if !bool::from(provided.as_slice().ct_eq(expected.as_slice())) {
            return reject("HMAC verification failed");
        }

        // Accepted: single-use nonce, leave safe mode, re-arm, clear streak.
        conn.execute(
            "DELETE FROM challenges WHERE device_id = ?1",
            params![device_id],
        )?;
        conn.execute(
            "UPDATE devices
                SET status = ?2, consecutive_resets = 0, window_started_ms = ?3,
                    last_feed_ms = ?3
             WHERE device_id = ?1",
            params![device_id, STATUS_RUNNING, now],
        )?;
        conn.execute(
            "UPDATE tasks SET advanced_this_window = 0 WHERE device_id = ?1",
            params![device_id],
        )?;
        conn.execute(
            "INSERT INTO clearance_records (device_id, at_ms, generation, accepted, detail)
             VALUES (?1, ?2, ?3, 1, 'accepted')",
            params![device_id, now, req.generation],
        )?;
        audit(
            &conn,
            device_id,
            now,
            "safe_mode_cleared",
            &format!("operator clearance accepted for generation {}", req.generation),
        )?;
        Ok(ClearResp {
            device_id: device_id.into(),
            cleared: true,
            status: STATUS_RUNNING.into(),
            generation: dev.fault_generation,
            detail: "safe mode cleared; watchdog re-armed".into(),
        })
    }

    // ---------------------------------------------------------------- status

    fn status_locked(&self, conn: &Connection, device_id: &str) -> Result<StatusResp, AppError> {
        let now = self.now();
        let dev = self.load_device(conn, device_id)?;
        let tasks = self.load_tasks(conn, device_id)?;
        let boot_count = self.boot_count(conn, device_id)?;
        let challenge_expires: Option<i64> = conn
            .query_row(
                "SELECT expires_ms FROM challenges WHERE device_id = ?1",
                params![device_id],
                |r| r.get(0),
            )
            .optional()?;
        let challenge_active = challenge_expires.is_some_and(|e| e >= now);
        let last_progress = dev
            .last_progress_json
            .as_deref()
            .and_then(|s| serde_json::from_str(s).ok());
        Ok(StatusResp {
            device_id: dev.device_id,
            status: dev.status,
            fault_generation: dev.fault_generation,
            consecutive_resets: dev.consecutive_resets,
            window_ms: dev.window_ms,
            reset_threshold: dev.reset_threshold,
            challenge_ttl_ms: dev.challenge_ttl_ms,
            window_started_ms: dev.window_started_ms,
            window_deadline_ms: deadline(dev.window_started_ms, dev.window_ms),
            last_feed_ms: dev.last_feed_ms,
            last_reset_reason: dev.last_reset_reason,
            last_reset_source: dev.last_reset_source,
            last_reset_at_ms: dev.last_reset_at_ms,
            last_progress,
            total_feeds: dev.total_feeds,
            total_resets: dev.total_resets,
            boot_count,
            challenge_active,
            tasks: tasks.iter().map(task_view).collect(),
        })
    }

    pub fn status(&self, device_id: &str) -> Result<StatusResp, AppError> {
        let conn = self.conn.lock().unwrap();
        self.status_locked(&conn, device_id)
    }

    pub fn list_resets(&self, device_id: &str) -> Result<ResetListResp, AppError> {
        let conn = self.conn.lock().unwrap();
        let _dev = self.load_device(&conn, device_id)?;
        let mut stmt = conn.prepare(
            "SELECT seq, at_ms, source, reason, boot_count, counted,
                    consecutive_after, fault_generation_after
             FROM reset_records WHERE device_id = ?1 ORDER BY seq",
        )?;
        let rows = stmt.query_map(params![device_id], |r| {
            Ok(ResetRecordView {
                seq: r.get(0)?,
                at_ms: r.get(1)?,
                source: r.get(2)?,
                reason: r.get(3)?,
                boot_count: r.get(4)?,
                counted: r.get::<_, i64>(5)? != 0,
                consecutive_after: r.get(6)?,
                fault_generation_after: r.get(7)?,
            })
        })?;
        Ok(ResetListResp {
            device_id: device_id.into(),
            resets: rows.collect::<rusqlite::Result<Vec<_>>>()?,
        })
    }

    pub fn list_events(&self, device_id: &str) -> Result<EventListResp, AppError> {
        let conn = self.conn.lock().unwrap();
        let _dev = self.load_device(&conn, device_id)?;
        let mut stmt = conn.prepare(
            "SELECT id, at_ms, event, detail FROM audit_log
             WHERE device_id = ?1 ORDER BY id",
        )?;
        let rows = stmt.query_map(params![device_id], |r| {
            Ok(EventView {
                id: r.get(0)?,
                at_ms: r.get(1)?,
                event: r.get(2)?,
                detail: r.get(3)?,
            })
        })?;
        Ok(EventListResp {
            device_id: device_id.into(),
            events: rows.collect::<rusqlite::Result<Vec<_>>>()?,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::wrapping_gt_u32;

    #[test]
    fn counter_ordering_handles_wraparound() {
        assert!(!wrapping_gt_u32(5, 5)); // equal == no progress
        assert!(wrapping_gt_u32(6, 5));
        assert!(!wrapping_gt_u32(4, 5));
        // wrap: 0 is "after" 0xFFFFFFFF by 1
        assert!(wrapping_gt_u32(0, u32::MAX));
        assert!(wrapping_gt_u32(1, u32::MAX - 100));
        // far jumps across half the space are treated as rollback, not advance
        assert!(!wrapping_gt_u32(0, 1u32 << 31));
    }
}
