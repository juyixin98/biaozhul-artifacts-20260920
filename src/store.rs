//! 配额存储核心。
//!
//! 持久化方式：append-only 事件日志（每行一个 JSON 事件），
//! 写入时 `flush + fsync`，启动时重放重建全部内存状态。
//! 所有状态转换都对应一条事件，因此「所有状态转换持久化」。
//!
//! 并发控制：`Mutex<Inner>` 单把锁串行化所有状态转换，
//! 保证「检查余量 -> 扣减预留 -> 落盘」是一个原子临界区，
//! 并发预留不可能共同越过同一剩余额度。

use crate::clock::Clock;
use crate::error::{AppResult, Error};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ReservationStatus {
    /// 已预留，占用预留额度
    Held,
    /// 已提交，转为实占
    Committed,
    /// 已取消，预留已释放
    Cancelled,
    /// 预留超时，系统自动释放
    Expired,
}

/// 事件日志中的一条状态转换。
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum Event {
    TenantCreated {
        tenant_id: String,
        quota_bytes: u64,
        quota_objects: u64,
        at_ms: i64,
    },
    Reserved {
        tenant_id: String,
        reservation_id: String,
        size_bytes: u64,
        objects: u64,
        expires_at_ms: i64,
        idempotency_key: Option<String>,
        at_ms: i64,
    },
    Committed {
        tenant_id: String,
        reservation_id: String,
        at_ms: i64,
    },
    Cancelled {
        tenant_id: String,
        reservation_id: String,
        at_ms: i64,
    },
    Expired {
        tenant_id: String,
        reservation_id: String,
        at_ms: i64,
    },
}

#[derive(Debug, Clone)]
struct Tenant {
    quota_bytes: u64,
    quota_objects: u64,
    committed_bytes: u64,
    committed_objects: u64,
}

#[derive(Debug, Clone)]
struct Reservation {
    tenant_id: String,
    reservation_id: String,
    size_bytes: u64,
    objects: u64,
    status: ReservationStatus,
    created_at_ms: i64,
    expires_at_ms: i64,
}

#[derive(Default)]
struct State {
    tenants: HashMap<String, Tenant>,
    reservations: HashMap<String, Reservation>,
    /// 幂等键 -> reservation key（仅成功的预留）
    idem: HashMap<String, String>,
    /// 租户 -> 当前活跃(Held)预留 key 集合
    active: HashMap<String, HashSet<String>>,
    /// 已追加事件数（也用于生成 ID 的去重分量）
    next_seq: u64,
}

struct Inner {
    writer: BufWriter<File>,
    state: State,
}

pub struct Store {
    inner: Mutex<Inner>,
    clock: Arc<dyn Clock>,
}

/// 对外的预留视图。
#[derive(Debug, Serialize)]
pub struct ReservationView {
    pub tenant_id: String,
    pub reservation_id: String,
    pub size_bytes: u64,
    pub objects: u64,
    pub status: ReservationStatus,
    pub created_at_ms: i64,
    pub expires_at_ms: i64,
}

/// 对外的租户配额视图。
#[derive(Debug, Serialize)]
pub struct TenantInfo {
    pub tenant_id: String,
    pub quota_bytes: u64,
    pub quota_objects: u64,
    pub committed_bytes: u64,
    pub committed_objects: u64,
    pub held_bytes: u64,
    pub held_objects: u64,
    pub active_reservations: usize,
}

/// 预留结果：视图 + 是否本次新建（幂等重放时为 false）。
pub type ReserveOutcome = (ReservationView, bool);

/// 取消结果：视图 + 是否本次真的释放（重复取消时为 false，不再次释放）。
pub type CancelOutcome = (ReservationView, bool);

fn res_key(tenant_id: &str, reservation_id: &str) -> String {
    format!("{tenant_id}\u{1f}{reservation_id}")
}

fn idem_key(tenant_id: &str, key: &str) -> String {
    format!("{tenant_id}\u{1f}k\u{1f}{key}")
}

/// 无外部依赖的随机-ish ID（纳秒时间 ^ 进程内递增计数器）。
fn gen_id(prefix: &str) -> String {
    static CTR: AtomicU64 = AtomicU64::new(0);
    let n = CTR.fetch_add(1, Ordering::Relaxed);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0);
    format!("{prefix}_{nanos:016x}_{:04x}", (n & 0xffff) as u16)
}

impl Reservation {
    fn view(&self) -> ReservationView {
        ReservationView {
            tenant_id: self.tenant_id.clone(),
            reservation_id: self.reservation_id.clone(),
            size_bytes: self.size_bytes,
            objects: self.objects,
            status: self.status,
            created_at_ms: self.created_at_ms,
            expires_at_ms: self.expires_at_ms,
        }
    }
}

impl Store {
    /// 打开（或创建）事件日志并重放。
    pub fn open(path: &Path, clock: Arc<dyn Clock>) -> AppResult<Arc<Store>> {
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent)?;
            }
        }
        let existed = path.exists();

        // 先重放旧日志（如有）。
        let mut state = State::default();
        if existed && std::fs::metadata(path)?.len() > 0 {
            let file = File::open(path)?;
            let reader = BufReader::new(file);
            let lines: Vec<String> = reader
                .lines()
                .collect::<Result<_, _>>()
                .map_err(|e| Error::Storage(format!("read event log: {e}")))?;
            let total = lines.len();
            for (idx, line) in lines.into_iter().enumerate() {
                if line.trim().is_empty() {
                    continue;
                }
                let event: Event = match serde_json::from_str(&line) {
                    Ok(ev) => ev,
                    // 末尾崩溃可能留下半行：容忍且仅容忍最后一行。
                    Err(_) if idx + 1 == total => break,
                    Err(e) => {
                        return Err(Error::Storage(format!(
                            "corrupt event log at line {}: {e}",
                            idx + 1
                        )))
                    }
                };
                apply_event(&mut state, &event);
            }
        }

        // 追加方式打开。
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)?;
        // 新建文件时尽力 fsync 目录，保证目录项落盘。
        if !existed {
            if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
                if let Ok(dir) = File::open(parent) {
                    let _ = dir.sync_all();
                }
            }
        }

        Ok(Arc::new(Store {
            inner: Mutex::new(Inner {
                writer: BufWriter::new(file),
                state,
            }),
            clock,
        }))
    }

    pub fn now_ms(&self) -> i64 {
        self.clock.now_ms()
    }

    /// 创建租户；租户已存在则 409。
    pub fn create_tenant(
        &self,
        tenant_id: &str,
        quota_bytes: u64,
        quota_objects: u64,
    ) -> AppResult<TenantInfo> {
        let now = self.clock.now_ms();
        let mut inner = self.inner.lock().expect("store mutex poisoned");
        if inner.state.tenants.contains_key(tenant_id) {
            return Err(Error::conflict(format!(
                "tenant '{tenant_id}' already exists"
            )));
        }
        let event = Event::TenantCreated {
            tenant_id: tenant_id.to_string(),
            quota_bytes,
            quota_objects,
            at_ms: now,
        };
        inner.append(&event)?;
        apply_event(&mut inner.state, &event);
        tenant_info_locked(&inner.state, tenant_id)
            .ok_or_else(|| Error::Storage("tenant missing after create".into()))
    }

    /// 预留。`reservation_id` 为 None 时服务端生成；
    /// 携带 `idempotency_key` 的重复请求返回原预留（created=false）。
    pub fn reserve(
        &self,
        tenant_id: &str,
        reservation_id: Option<String>,
        size_bytes: u64,
        objects: u64,
        ttl_ms: i64,
        idempotency_key: Option<String>,
    ) -> AppResult<ReserveOutcome> {
        if size_bytes == 0 {
            return Err(Error::bad_request("size_bytes must be > 0"));
        }
        if objects == 0 {
            return Err(Error::bad_request("objects must be > 0"));
        }
        if ttl_ms <= 0 {
            return Err(Error::bad_request("ttl_ms must be > 0"));
        }

        let now = self.clock.now_ms();
        let mut inner = self.inner.lock().expect("store mutex poisoned");

        // 1) 幂等重放优先（即使原预留已过期，也原样返回，不重复扣减）。
        if let Some(key) = idempotency_key.as_deref() {
            if let Some(rkey) = inner.state.idem.get(&idem_key(tenant_id, key)) {
                let r = inner
                    .state
                    .reservations
                    .get(rkey)
                    .ok_or_else(|| Error::Storage("idempotency index dangling".into()))?;
                return Ok((r.view(), false));
            }
        }

        // 2) 惰性过期：先释放本租户已到期预留，再判余量。
        expire_tenant_locked(&mut inner, tenant_id, now)?;

        // 3) 租户必须存在。
        let tenant = inner
            .state
            .tenants
            .get(tenant_id)
            .ok_or_else(|| Error::not_found(format!("tenant '{tenant_id}' not found")))?;

        // 4) 显式 ID 冲突检查。
        let rid = match reservation_id {
            Some(rid) => {
                if inner
                    .state
                    .reservations
                    .contains_key(&res_key(tenant_id, &rid))
                {
                    return Err(Error::conflict(format!(
                        "reservation '{rid}' already exists for tenant '{tenant_id}'"
                    )));
                }
                rid
            }
            None => loop {
                let cand = gen_id("rsv");
                if !inner
                    .state
                    .reservations
                    .contains_key(&res_key(tenant_id, &cand))
                {
                    break cand;
                }
            },
        };

        // 5) 配额检查：committed + held（不含本次）+ want <= limit，字节与对象数同时满足。
        let (held_bytes, held_objects) = held_locked(&inner.state, tenant_id);
        let used_bytes = tenant.committed_bytes;
        let used_objects = tenant.committed_objects;
        let quota_bytes = tenant.quota_bytes;
        let quota_objects = tenant.quota_objects;

        let over_bytes = used_bytes.saturating_add(held_bytes).saturating_add(size_bytes)
            > quota_bytes;
        let over_objects = used_objects
            .saturating_add(held_objects)
            .saturating_add(objects)
            > quota_objects;
        if over_bytes || over_objects {
            return Err(Error::QuotaExceeded {
                detail: format!(
                    "reservation would exceed tenant quota (bytes over: {over_bytes}, objects over: {over_objects})"
                ),
                limit_bytes: quota_bytes,
                used_bytes,
                held_bytes,
                want_bytes: size_bytes,
                limit_objects: quota_objects,
                used_objects,
                held_objects,
                want_objects: objects,
            });
        }

        // 6) 落盘 + 生效。
        let expires_at_ms = now.saturating_add(ttl_ms);
        let event = Event::Reserved {
            tenant_id: tenant_id.to_string(),
            reservation_id: rid.clone(),
            size_bytes,
            objects,
            expires_at_ms,
            idempotency_key: idempotency_key.clone(),
            at_ms: now,
        };
        inner.append(&event)?;
        apply_event(&mut inner.state, &event);

        let view = inner
            .state
            .reservations
            .get(&res_key(tenant_id, &rid))
            .expect("just inserted")
            .view();
        Ok((view, true))
    }

    /// 提交：预留(Held) -> 实占(Committed)。重复提交幂等返回原记录。
    pub fn commit(&self, tenant_id: &str, reservation_id: &str) -> AppResult<ReservationView> {
        let now = self.clock.now_ms();
        let mut inner = self.inner.lock().expect("store mutex poisoned");
        expire_tenant_locked(&mut inner, tenant_id, now)?;

        let key = res_key(tenant_id, reservation_id);
        let r = inner
            .state
            .reservations
            .get(&key)
            .ok_or_else(|| Error::not_found(format!("reservation '{reservation_id}' not found")))?;
        match r.status {
            ReservationStatus::Committed => Ok(r.view()),
            ReservationStatus::Cancelled => Err(Error::conflict(
                "reservation already cancelled; cannot commit".to_string(),
            )),
            ReservationStatus::Expired => Err(Error::expired(
                "reservation has expired; held quota was released".to_string(),
            )),
            ReservationStatus::Held => {
                let event = Event::Committed {
                    tenant_id: tenant_id.to_string(),
                    reservation_id: reservation_id.to_string(),
                    at_ms: now,
                };
                inner.append(&event)?;
                apply_event(&mut inner.state, &event);
                Ok(inner.state.reservations.get(&key).expect("present").view())
            }
        }
    }

    /// 取消：预留(Held) -> 已取消，释放预留额度。
    /// 重复取消幂等：不再次释放（返回 created=false 风格的 released=false）。
    pub fn cancel(&self, tenant_id: &str, reservation_id: &str) -> AppResult<CancelOutcome> {
        let now = self.clock.now_ms();
        let mut inner = self.inner.lock().expect("store mutex poisoned");
        expire_tenant_locked(&mut inner, tenant_id, now)?;

        let key = res_key(tenant_id, reservation_id);
        let r = inner
            .state
            .reservations
            .get(&key)
            .ok_or_else(|| Error::not_found(format!("reservation '{reservation_id}' not found")))?;
        match r.status {
            ReservationStatus::Cancelled => Ok((r.view(), false)),
            ReservationStatus::Committed => Err(Error::conflict(
                "reservation already committed; cannot cancel".to_string(),
            )),
            ReservationStatus::Expired => Err(Error::expired(
                "reservation already expired; quota was already released".to_string(),
            )),
            ReservationStatus::Held => {
                let event = Event::Cancelled {
                    tenant_id: tenant_id.to_string(),
                    reservation_id: reservation_id.to_string(),
                    at_ms: now,
                };
                inner.append(&event)?;
                apply_event(&mut inner.state, &event);
                Ok((inner.state.reservations.get(&key).expect("present").view(), true))
            }
        }
    }

    /// 全量扫描并释放所有已到期预留，返回释放条数。后台任务与管理接口调用。
    pub fn expire_due(&self) -> AppResult<usize> {
        let now = self.clock.now_ms();
        let mut inner = self.inner.lock().expect("store mutex poisoned");

        // 先收集到期 key（分组按租户，仅为复用 expire 路径）。
        let mut due: Vec<(String, String)> = Vec::new();
        for (tenant_id, keys) in &inner.state.active {
            for rkey in keys {
                if let Some(r) = inner.state.reservations.get(rkey) {
                    if r.status == ReservationStatus::Held && r.expires_at_ms <= now {
                        due.push((tenant_id.clone(), r.reservation_id.clone()));
                    }
                }
            }
        }
        let n = due.len();
        for (tenant_id, rid) in due {
            let event = Event::Expired {
                tenant_id,
                reservation_id: rid,
                at_ms: now,
            };
            inner.append(&event)?;
            apply_event(&mut inner.state, &event);
        }
        Ok(n)
    }

    pub fn tenant_info(&self, tenant_id: &str) -> AppResult<TenantInfo> {
        let now = self.clock.now_ms();
        let mut inner = self.inner.lock().expect("store mutex poisoned");
        expire_tenant_locked(&mut inner, tenant_id, now)?;
        tenant_info_locked(&inner.state, tenant_id)
            .ok_or_else(|| Error::not_found(format!("tenant '{tenant_id}' not found")))
    }

    pub fn reservation(
        &self,
        tenant_id: &str,
        reservation_id: &str,
    ) -> AppResult<ReservationView> {
        let now = self.clock.now_ms();
        let mut inner = self.inner.lock().expect("store mutex poisoned");
        expire_tenant_locked(&mut inner, tenant_id, now)?;
        inner
            .state
            .reservations
            .get(&res_key(tenant_id, reservation_id))
            .map(|r| r.view())
            .ok_or_else(|| Error::not_found(format!("reservation '{reservation_id}' not found")))
    }
}

impl Inner {
    /// 追加一条事件并 fsync（在持锁状态下调用，因此写入顺序即状态顺序）。
    fn append(&mut self, event: &Event) -> AppResult<()> {
        serde_json::to_writer(&mut self.writer, event)
            .map_err(|e| Error::Storage(format!("encode event: {e}")))?;
        self.writer
            .write_all(b"\n")
            .map_err(|e| Error::Storage(format!("write event: {e}")))?;
        self.writer
            .flush()
            .map_err(|e| Error::Storage(format!("flush event: {e}")))?;
        self.writer
            .get_ref()
            .sync_all()
            .map_err(|e| Error::Storage(format!("fsync event: {e}")))?;
        self.state.next_seq += 1;
        Ok(())
    }
}

/// 纯函数：把一条事件应用到内存状态。重放与在线写入共用同一逻辑。
fn apply_event(state: &mut State, event: &Event) {
    match event {
        Event::TenantCreated {
            tenant_id,
            quota_bytes,
            quota_objects,
            ..
        } => {
            state.tenants.insert(
                tenant_id.clone(),
                Tenant {
                    quota_bytes: *quota_bytes,
                    quota_objects: *quota_objects,
                    committed_bytes: 0,
                    committed_objects: 0,
                },
            );
            state.active.insert(tenant_id.clone(), HashSet::new());
        }
        Event::Reserved {
            tenant_id,
            reservation_id,
            size_bytes,
            objects,
            expires_at_ms,
            idempotency_key,
            at_ms,
        } => {
            let r = Reservation {
                tenant_id: tenant_id.clone(),
                reservation_id: reservation_id.clone(),
                size_bytes: *size_bytes,
                objects: *objects,
                status: ReservationStatus::Held,
                created_at_ms: *at_ms,
                expires_at_ms: *expires_at_ms,
            };
            state
                .reservations
                .insert(res_key(tenant_id, reservation_id), r);
            if let Some(k) = idempotency_key {
                state
                    .idem
                    .insert(idem_key(tenant_id, k), res_key(tenant_id, reservation_id));
            }
            if let Some(set) = state.active.get_mut(tenant_id) {
                set.insert(res_key(tenant_id, reservation_id));
            }
        }
        Event::Committed {
            tenant_id,
            reservation_id,
            ..
        } => {
            let key = res_key(tenant_id, reservation_id);
            if let Some(r) = state.reservations.get_mut(&key) {
                if r.status == ReservationStatus::Held {
                    r.status = ReservationStatus::Committed;
                    if let Some(t) = state.tenants.get_mut(tenant_id) {
                        t.committed_bytes = t.committed_bytes.saturating_add(r.size_bytes);
                        t.committed_objects = t.committed_objects.saturating_add(r.objects);
                    }
                }
            }
            if let Some(set) = state.active.get_mut(tenant_id) {
                set.remove(&key);
            }
        }
        Event::Cancelled {
            tenant_id,
            reservation_id,
            ..
        } => {
            let key = res_key(tenant_id, reservation_id);
            if let Some(r) = state.reservations.get_mut(&key) {
                if r.status == ReservationStatus::Held {
                    r.status = ReservationStatus::Cancelled;
                }
            }
            if let Some(set) = state.active.get_mut(tenant_id) {
                set.remove(&key);
            }
        }
        Event::Expired {
            tenant_id,
            reservation_id,
            ..
        } => {
            let key = res_key(tenant_id, reservation_id);
            if let Some(r) = state.reservations.get_mut(&key) {
                if r.status == ReservationStatus::Held {
                    r.status = ReservationStatus::Expired;
                }
            }
            if let Some(set) = state.active.get_mut(tenant_id) {
                set.remove(&key);
            }
        }
    }
}

/// 惰性过期：释放某租户当前已到期的 Held 预留（调用方持锁）。
fn expire_tenant_locked(inner: &mut Inner, tenant_id: &str, now: i64) -> AppResult<()> {
    let due: Vec<String> = match inner.state.active.get(tenant_id) {
        Some(set) => set
            .iter()
            .filter_map(|k| inner.state.reservations.get(k))
            .filter(|r| r.status == ReservationStatus::Held && r.expires_at_ms <= now)
            .map(|r| r.reservation_id.clone())
            .collect(),
        None => return Ok(()),
    };
    for rid in due {
        let event = Event::Expired {
            tenant_id: tenant_id.to_string(),
            reservation_id: rid,
            at_ms: now,
        };
        inner.append(&event)?;
        apply_event(&mut inner.state, &event);
    }
    Ok(())
}

fn held_locked(state: &State, tenant_id: &str) -> (u64, u64) {
    let mut bytes = 0u64;
    let mut objects = 0u64;
    if let Some(set) = state.active.get(tenant_id) {
        for k in set {
            if let Some(r) = state.reservations.get(k) {
                bytes = bytes.saturating_add(r.size_bytes);
                objects = objects.saturating_add(r.objects);
            }
        }
    }
    (bytes, objects)
}

fn tenant_info_locked(state: &State, tenant_id: &str) -> Option<TenantInfo> {
    let tenant = state.tenants.get(tenant_id)?;
    let (held_bytes, held_objects) = held_locked(state, tenant_id);
    let active_reservations = state
        .active
        .get(tenant_id)
        .map(|s| s.len())
        .unwrap_or(0);
    Some(TenantInfo {
        tenant_id: tenant_id.to_string(),
        quota_bytes: tenant.quota_bytes,
        quota_objects: tenant.quota_objects,
        committed_bytes: tenant.committed_bytes,
        committed_objects: tenant.committed_objects,
        held_bytes,
        held_objects,
        active_reservations,
    })
}
