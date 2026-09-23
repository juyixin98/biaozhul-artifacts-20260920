//! 锁引擎：区间锁、共享/排他、等待队列、事务中止、等待图与确定性死锁检测。
//!
//! ## 设计
//!
//! ### 区间
//! 所有区间统一为**左闭右开** `[start, end)`，要求 `start < end`。两个区间
//! 重叠当且仅当 `a.start < b.end && b.start < a.end`。因此 `[0,10)` 与
//! `[10,20)` 相邻但**不重叠**，互不阻塞（用于验收“相邻不误报”）。
//!
//! ### 锁模式
//! - 共享（S，shared）：多个事务可同时持有同一重叠区间上的 S 锁。
//! - 排他（X，exclusive）：与任何模式互斥。
//! 兼容矩阵仅 S/S 兼容。
//!
//! ### 等待队列（每资源一条 FIFO）
//! 每个资源独立维护按请求顺序排列的等待队列。引擎采用“公平队列”策略：
//! 队首等待者之后的新请求**不允许插队**。一个等待者 `w` 在某次推进中
//! 可以被授予，当且仅当同时满足：
//! 1. 当前没有任何*其他*事务持有与 `w` 重叠且模式冲突的锁；
//! 2. 队列中排在 `w` **前面**的等待者，没有与 `w` 重叠且模式冲突的请求。
//! 被授予的等待者从队列移除并加入持有者集合。
//!
//! ### 等待图（waits-for graph）
//! 图的节点是事务，边 `w -> T` 表示 `w` 因 `T` 而**真实阻塞**。边分两类，
//! 与上面的授予条件一一对应（条件不满足才有边），因此图中不存在“假边”：
//! - **持有者边**：`T` 当前持有与 `w` 重叠且冲突的锁；
//! - **队列边**：`T` 是队列中排在 `w` 前面、与 `w` 重叠且冲突的等待者。
//! 自环不会出现（同一事务对自身的请求：已持有重叠锁时会走授予/升级路径，
//! 不会对自己建边）。
//!
//! ### 死锁检测与确定性牺牲者
//! 每次有请求进入等待状态后，重建等待图并用 Tarjan 算法求强连通分量。
//! 仅把“大小 >= 2 的 SCC”视为死锁环（自等待不可能发生，故不考虑自环）。
//! 消解过程确定性且可终止：每一轮取所有环节点中 **事务 id 字典序最大**
//! 者作为牺牲者（victim）将其**中止**——释放它持有的全部锁、删除它的
//! 全部等待请求并推进等待队列——然后重建等待图；若残图仍有环则重复
//! （因为新增的边只从本次请求者出发，通常一轮即可解开）。
//! 全部检测到的环与牺牲者序列由触发等待的那次 [`Engine::acquire`] 同步返回。
//!
//! ### 锁升级
//! 事务已持有某资源上的 S 锁、再次请求 X 锁时按升级处理：
//! - 若 X 区间可立即获得（无其他持有者冲突，且队列前方无冲突等待者），
//!   则升级成功（锁记录合并为覆盖两段区间的 X 锁，保守合并以免拆分状态机）；
//! - 否则该 X 请求进入等待队列（带 `upgrade: true` 标记），并正常参与
//!   等待图与死锁检测。等待期间其原有 S 锁仍然保留（真实数据库的典型
//!   语义），因此别的 X 请求会因它而阻塞——升级请求自身也可能成为环上的
//!   一环，死锁检测照常生效。

use std::collections::{BTreeMap, BTreeSet};

use serde::Serialize;

/// 锁模式。
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Mode {
    /// 共享锁。
    Shared,
    /// 排他锁。
    Exclusive,
}

impl Mode {
    /// 两种模式在重叠区间上是否兼容。仅 S/S 兼容。
    pub fn compatible(a: Mode, b: Mode) -> bool {
        matches!((a, b), (Mode::Shared, Mode::Shared))
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Mode::Shared => "shared",
            Mode::Exclusive => "exclusive",
        }
    }

    pub fn parse(s: &str) -> Result<Mode, EngineError> {
        match s {
            "shared" | "s" | "S" | "SHARED" => Ok(Mode::Shared),
            "exclusive" | "x" | "X" | "EXCLUSIVE" => Ok(Mode::Exclusive),
            other => Err(EngineError::BadMode(other.to_string())),
        }
    }
}

/// 一个半开区间 `[start, end)`。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Interval {
    pub start: i64,
    pub end: i64,
}

impl Interval {
    pub fn new(start: i64, end: i64) -> Result<Interval, EngineError> {
        if start >= end {
            return Err(EngineError::BadInterval { start, end });
        }
        Ok(Interval { start, end })
    }

    /// 两个左闭右开区间是否重叠。相邻（端点相接）不算重叠。
    pub fn overlaps(&self, other: &Interval) -> bool {
        self.start < other.end && other.start < self.end
    }
}

/// 已授予的锁。
#[derive(Clone, Debug)]
pub struct HeldLock {
    pub txn: String,
    pub mode: Mode,
    pub interval: Interval,
}

/// 等待队列中的请求。
#[derive(Clone, Debug)]
pub struct Waiter {
    pub txn: String,
    pub mode: Mode,
    pub interval: Interval,
    /// 是否为 S -> X 升级请求。
    pub upgrade: bool,
}

/// 单个资源的锁状态。
#[derive(Clone, Debug, Default)]
pub struct ResourceState {
    /// 已授予的锁。
    held: Vec<HeldLock>,
    /// FIFO 等待队列（队首在前）。
    pending: Vec<Waiter>,
}

/// 引擎返回的错误。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EngineError {
    TxnAborted { txn: String },
    BadInterval { start: i64, end: i64 },
    BadMode(String),
    NotFound(String),
}

impl std::fmt::Display for EngineError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            EngineError::TxnAborted { txn } => write!(f, "transaction {txn} was aborted as a deadlock victim"),
            EngineError::BadInterval { start, end } => {
                write!(f, "invalid interval [{start}, {end}): require start < end")
            }
            EngineError::BadMode(m) => write!(f, "unknown lock mode {m:?}, want shared|exclusive"),
            EngineError::NotFound(s) => write!(f, "{s} not found"),
        }
    }
}

impl std::error::Error for EngineError {}

/// 一次加锁调用后触发的死锁信息（若有）。
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct DeadlockInfo {
    /// 首次检测到的所有死锁环（每个环为事务 id 的有序列表，已排序、去重呈现）。
    pub cycles: Vec<Vec<String>>,
    /// 本次为消解死锁而依次中止的牺牲者（通常只有一个；
    /// 极少数情况下单个牺牲者无法破除残图中的其他环，故为列表）。
    pub victims: Vec<String>,
}

/// 一次加锁调用的结果报告。
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct AcquireReport {
    pub granted: bool,
    pub queued: bool,
    /// true 表示这是一次当场完成的 S->X 升级。
    pub upgraded: bool,
    /// 若本次入队检测到死锁，这里给出检测到的环与为消解而中止的牺牲者序列；
    /// 调用者本人是否在牺牲者序列中决定了它是被中止还是继续等待/已获锁。
    pub deadlock: Option<DeadlockInfo>,
}

/// 等待图中一条边的视图。
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct EdgeView {
    pub from: String,
    pub to: String,
    /// holder = 被持有者阻塞；ahead = 被队列前方等待者阻塞。
    pub kind: String,
    pub resource: String,
}

/// 完整等待图视图。
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct WaitsView {
    pub edges: Vec<EdgeView>,
}

/// 单个锁的视图。
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct LockView {
    pub txn: String,
    pub mode: String,
    pub start: i64,
    pub end: i64,
}

/// 单个等待者的视图。
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct WaiterView {
    pub txn: String,
    pub mode: String,
    pub start: i64,
    pub end: i64,
    pub upgrade: bool,
}

/// 单个资源的视图。
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ResourceView {
    pub resource: String,
    pub held: Vec<LockView>,
    pub waiting: Vec<WaiterView>,
}

/// 单个事务的视图。
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct TxnView {
    pub txn: String,
    pub status: String,
    pub locks: Vec<(String, LockView)>,
    pub waiting_on: Vec<(String, WaiterView)>,
}

/// 锁引擎。方法全部为同步方法（`&mut self`），由外层（HTTP 层或测试）
/// 自行加锁；这让核心逻辑确定、易测。
#[derive(Debug, Default)]
pub struct Engine {
    /// 所有资源的锁状态。
    resources: BTreeMap<String, ResourceState>,
    /// 活跃事务集合（曾 begin 且未结束）。
    active: BTreeSet<String>,
    /// 已中止事务集合（保留到 cleanup，便于查询“死因”）。
    aborted: BTreeSet<String>,
}

impl Engine {
    pub fn new() -> Engine {
        Engine::default()
    }

    /// 开启事务。
    pub fn begin(&mut self, txn: &str) -> Result<(), EngineError> {
        if self.aborted.contains(txn) {
            return Err(EngineError::TxnAborted { txn: txn.to_string() });
        }
        self.active.insert(txn.to_string());
        Ok(())
    }

    /// 事务是否活跃。
    pub fn is_active(&self, txn: &str) -> bool {
        self.active.contains(txn)
    }

    /// 加锁（区间左闭右开）。
    ///
    /// - 能立即授予则授予；
    /// - 否则进入该资源的 FIFO 等待队列，并运行死锁检测与消解；
    /// - 若本事务被选为牺牲者，则当场中止，返回的 report 中带 deadlock 且
    ///   `granted/queued` 均为 false（HTTP 层据此返回 409）。
    pub fn acquire(
        &mut self,
        txn: &str,
        resource: &str,
        mode: Mode,
        start: i64,
        end: i64,
    ) -> Result<AcquireReport, EngineError> {
        self.check_live(txn)?;
        let interval = Interval::new(start, end)?;

        let mut upgrade_request = false;
        let state = self.resources.entry(resource.to_string()).or_default();

        // ---- 同事务重入 / 升级判定 ----
        let owns_overlapping = state
            .held
            .iter()
            .any(|l| l.txn == txn && l.interval.overlaps(&interval));
        if owns_overlapping {
            // 已持有 X，或再次请求 S：直接视为授予（S 被 X 覆盖；X 已最强）。
            // 已持有 S 且请求 X => 升级。
            let has_x = state
                .held
                .iter()
                .any(|l| l.txn == txn && l.mode == Mode::Exclusive && l.interval.overlaps(&interval));
            if has_x || mode == Mode::Shared {
                return Ok(AcquireReport { granted: true, queued: false, upgraded: false, deadlock: None });
            }
            // S -> X 升级。
            upgrade_request = true;
            let blocked_by_holder = state.held.iter().any(|l| {
                l.txn != txn && l.interval.overlaps(&interval) && !Mode::compatible(Mode::Exclusive, l.mode)
            });
            let blocked_ahead = Self::blocked_by_ahead(state, &Waiter {
                txn: txn.to_string(),
                mode: Mode::Exclusive,
                interval,
                upgrade: true,
            });
            if !blocked_by_holder && blocked_ahead.is_empty() {
                // 当场升级：把该事务在此资源上的锁合并为一个覆盖性 X 锁。
                Self::merge_upgrade(state, txn, interval);
                return Ok(AcquireReport { granted: true, queued: false, upgraded: true, deadlock: None });
            }
            // 升级需要等待：入队（保留原 S 锁）。
            state.pending.push(Waiter {
                txn: txn.to_string(),
                mode: Mode::Exclusive,
                interval,
                upgrade: true,
            });
        } else {
            // ---- 全新请求 ----
            // 排队策略：若队列中已有等待者，新请求排在其后（公平、不插队），
            // 是否阻塞由 blocked_by_holder / blocked_ahead 统一判定。
            let blocked_by_holder = state.held.iter().any(|l| {
                l.txn != txn && l.interval.overlaps(&interval) && !Mode::compatible(mode, l.mode)
            });
            let probe = Waiter { txn: txn.to_string(), mode, interval, upgrade: false };
            let blocked_ahead = Self::blocked_by_ahead(state, &probe);
            if !blocked_by_holder && blocked_ahead.is_empty() {
                state.held.push(HeldLock { txn: txn.to_string(), mode, interval });
                return Ok(AcquireReport { granted: true, queued: false, upgraded: false, deadlock: None });
            }
            state.pending.push(probe);
        }

        // ---- 进入等待：死锁检测与消解 ----
        // 不变量：进入本次请求前等待图无环（此前每次入队都已把环消解）。
        // 本次只新增了从调用者 txn 出发的边，因此新图的所有环都包含 txn。
        // 确定性策略：反复“取所有环节点中 id 字典序最大者中止 → 重建图”，
        // 直到图中无环。通常一次即可；若首个牺牲者不是 txn，残图可能仍成环。
        let first_cycles = find_cycles(&self.build_waits(), &self.active);
        if first_cycles.is_empty() {
            return Ok(AcquireReport { granted: false, queued: true, upgraded: false, deadlock: None });
        }

        let mut cycles = first_cycles.clone();
        let mut victims: Vec<String> = Vec::new();
        loop {
            let victim = cycles
                .iter()
                .flatten()
                .max()
                .expect("cycles non-empty")
                .clone();
            victims.push(victim.clone());
            self.abort_txn(&victim);
            let next = find_cycles(&self.build_waits(), &self.active);
            if next.is_empty() {
                break;
            }
            cycles = next;
        }
        let self_aborted = self.aborted.contains(txn);
        let info = DeadlockInfo { cycles: first_cycles, victims };

        if self_aborted {
            // 调用者本人被中止：其全部锁与本次等待请求都已清除。
            return Ok(AcquireReport {
                granted: false,
                queued: false,
                upgraded: false,
                deadlock: Some(info),
            });
        }

        // 调用者存活：中止他人后队列已推进。判断本次请求的最终状态。
        let state = self.resources.get(resource).expect("resource exists");
        let still_waiting = state
            .pending
            .iter()
            .any(|w| w.txn == txn && w.interval == interval);
        if still_waiting {
            return Ok(AcquireReport {
                granted: false,
                queued: true,
                upgraded: false,
                deadlock: Some(info),
            });
        }
        let holds_x = state.held.iter().any(|l| {
            l.txn == txn && l.mode == Mode::Exclusive && l.interval.overlaps(&interval)
        });
        // 非升级请求被兑现 => granted；升级请求被兑现 => granted + upgraded。
        Ok(AcquireReport {
            granted: true,
            queued: false,
            upgraded: upgrade_request && holds_x,
            deadlock: Some(info),
        })
    }

    /// 提交事务：释放其持有的全部锁、删除其等待请求，推进队列。
    pub fn commit(&mut self, txn: &str) -> Result<(), EngineError> {
        self.check_live(txn)?;
        self.active.remove(txn);
        self.remove_locks_and_waits(txn);
        self.advance_all();
        Ok(())
    }

    /// 显式中止事务（非死锁原因），同样释放全部锁并推进队列。
    pub fn abort(&mut self, txn: &str) -> Result<(), EngineError> {
        self.check_live(txn)?;
        self.abort_txn(txn);
        Ok(())
    }

    /// 内部：把事务标记为中止并清理。
    fn abort_txn(&mut self, txn: &str) {
        self.active.remove(txn);
        self.aborted.insert(txn.to_string());
        self.remove_locks_and_waits(txn);
        self.advance_all();
    }

    /// 清除已中止事务的墓碑记录（之后可以用同 id 重新 begin）。
    pub fn cleanup(&mut self, txn: &str) {
        self.aborted.remove(txn);
    }

    fn check_live(&self, txn: &str) -> Result<(), EngineError> {
        if self.aborted.contains(txn) {
            return Err(EngineError::TxnAborted { txn: txn.to_string() });
        }
        if !self.active.contains(txn) {
            return Err(EngineError::NotFound(format!("transaction {txn}")));
        }
        Ok(())
    }

    /// 删除事务在所有资源上的持锁与等待请求。
    fn remove_locks_and_waits(&mut self, txn: &str) {
        for state in self.resources.values_mut() {
            state.held.retain(|l| l.txn != txn);
            state.pending.retain(|w| w.txn != txn);
        }
    }

    /// 判断等待者 `w` 是否被“队列前方”的等待者阻塞，返回阻塞者事务集合。
    /// 只读取给定资源的队列，故为关联函数（避免与 `&mut ResourceState` 借用冲突）。
    fn blocked_by_ahead(state: &ResourceState, w: &Waiter) -> Vec<String> {
        let mut hit = Vec::new();
        for ahead in &state.pending {
            if ahead.txn == w.txn {
                // 同一事务的旧请求排在前面（如先 S 等待、又请求 X）：
                // 不构成自阻塞。
                continue;
            }
            if ahead.interval.overlaps(&w.interval) && !Mode::compatible(w.mode, ahead.mode) {
                if !hit.contains(&ahead.txn) {
                    hit.push(ahead.txn.clone());
                }
            }
        }
        hit
    }

    /// S->X 当场升级：把该事务在该资源上的锁替换为覆盖原区间与新区间的 X 锁。
    fn merge_upgrade(state: &mut ResourceState, txn: &str, interval: Interval) {
        let mut start = interval.start;
        let mut end = interval.end;
        state.held.retain(|l| {
            if l.txn == txn {
                start = start.min(l.interval.start);
                end = end.max(l.interval.end);
                false
            } else {
                true
            }
        });
        state.held.push(HeldLock {
            txn: txn.to_string(),
            mode: Mode::Exclusive,
            interval: Interval { start, end },
        });
    }

    /// 推进所有资源的等待队列（反复尝试授予队首/后续可授予者）。
    fn advance_all(&mut self) {
        for state in self.resources.values_mut() {
            loop {
                let mut progress = false;
                let mut i = 0;
                while i < state.pending.len() {
                    // 暂借取出第 i 个等待者做判定。
                    let w = state.pending[i].clone();
                    let blocked_holder = state.held.iter().any(|l| {
                        l.txn != w.txn
                            && l.interval.overlaps(&w.interval)
                            && !Mode::compatible(w.mode, l.mode)
                    });
                    // 队列前方 = pending[..i]。
                    let ahead: Vec<Waiter> = state.pending[..i].to_vec();
                    let blocked_ahead = ahead.iter().any(|a| {
                        a.txn != w.txn
                            && a.interval.overlaps(&w.interval)
                            && !Mode::compatible(w.mode, a.mode)
                    });
                    if !blocked_holder && !blocked_ahead {
                        state.pending.remove(i);
                        if w.upgrade {
                            // 升级请求兑现：合并该事务现有锁为 X。
                            let mut start = w.interval.start;
                            let mut end = w.interval.end;
                            state.held.retain(|l| {
                                if l.txn == w.txn {
                                    start = start.min(l.interval.start);
                                    end = end.max(l.interval.end);
                                    false
                                } else {
                                    true
                                }
                            });
                            state.held.push(HeldLock {
                                txn: w.txn.clone(),
                                mode: Mode::Exclusive,
                                interval: Interval { start, end },
                            });
                        } else {
                            state.held.push(HeldLock {
                                txn: w.txn,
                                mode: w.mode,
                                interval: w.interval,
                            });
                        }
                        progress = true;
                        // 不递增 i：后继元素前移，继续检查同一下标。
                    } else {
                        i += 1;
                    }
                }
                if !progress {
                    break;
                }
            }
        }
    }

    /// 重建等待图。边集合用 BTreeSet 去重并保持确定顺序。
    pub fn build_waits(&self) -> Vec<EdgeView> {
        let mut edges: BTreeSet<(String, String, &'static str, String)> = BTreeSet::new();
        for (resource, state) in &self.resources {
            // 队列边：每个等待者 -> 其前方冲突等待者。
            for (i, w) in state.pending.iter().enumerate() {
                for a in &state.pending[..i] {
                    if a.txn != w.txn
                        && a.interval.overlaps(&w.interval)
                        && !Mode::compatible(w.mode, a.mode)
                    {
                        edges.insert((w.txn.clone(), a.txn.clone(), "ahead", resource.clone()));
                    }
                }
                // 持有者边：等待者 -> 持有冲突锁的事务。
                for h in &state.held {
                    if h.txn != w.txn
                        && h.interval.overlaps(&w.interval)
                        && !Mode::compatible(w.mode, h.mode)
                    {
                        edges.insert((w.txn.clone(), h.txn.clone(), "holder", resource.clone()));
                    }
                }
            }
        }
        edges
            .into_iter()
            .map(|(from, to, kind, resource)| EdgeView {
                from,
                to,
                kind: kind.to_string(),
                resource,
            })
            .collect()
    }

    pub fn waits_view(&self) -> WaitsView {
        WaitsView { edges: self.build_waits() }
    }

    /// 全部资源视图（按资源 id 排序）。
    pub fn resources_view(&self) -> Vec<ResourceView> {
        self.resources
            .iter()
            .map(|(r, s)| ResourceView {
                resource: r.clone(),
                held: s
                    .held
                    .iter()
                    .map(|l| LockView {
                        txn: l.txn.clone(),
                        mode: l.mode.as_str().to_string(),
                        start: l.interval.start,
                        end: l.interval.end,
                    })
                    .collect(),
                waiting: s
                    .pending
                    .iter()
                    .map(|w| WaiterView {
                        txn: w.txn.clone(),
                        mode: w.mode.as_str().to_string(),
                        start: w.interval.start,
                        end: w.interval.end,
                        upgrade: w.upgrade,
                    })
                    .collect(),
            })
            .collect()
    }

    /// 单事务视图。
    pub fn txn_view(&self, txn: &str) -> Option<TxnView> {
        let status = if self.active.contains(txn) {
            "active"
        } else if self.aborted.contains(txn) {
            "aborted"
        } else {
            return None;
        };
        let mut locks = Vec::new();
        let mut waiting_on = Vec::new();
        for (r, s) in &self.resources {
            for l in &s.held {
                if l.txn == txn {
                    locks.push((r.clone(), LockView {
                        txn: l.txn.clone(),
                        mode: l.mode.as_str().to_string(),
                        start: l.interval.start,
                        end: l.interval.end,
                    }));
                }
            }
            for w in &s.pending {
                if w.txn == txn {
                    waiting_on.push((r.clone(), WaiterView {
                        txn: w.txn.clone(),
                        mode: w.mode.as_str().to_string(),
                        start: w.interval.start,
                        end: w.interval.end,
                        upgrade: w.upgrade,
                    }));
                }
            }
        }
        Some(TxnView { txn: txn.to_string(), status: status.to_string(), locks, waiting_on })
    }

    /// 全部事务 id 及状态。
    pub fn txns(&self) -> BTreeMap<String, String> {
        let mut out = BTreeMap::new();
        for t in &self.active {
            out.insert(t.clone(), "active".to_string());
        }
        for t in &self.aborted {
            out.insert(t.clone(), "aborted".to_string());
        }
        out
    }
}

/// 在等待图中找出所有死锁环。
///
/// 用 Tarjan 求强连通分量；大小 >= 2 的 SCC 即为环（其内部节点互相等待）。
/// 为让输出确定性：邻接表按目标 id 排序，结果环按其（排序后的）节点列表排序。
fn find_cycles(edges: &[EdgeView], nodes: &BTreeSet<String>) -> Vec<Vec<String>> {
    // 邻接表（去重）。
    let mut adj: BTreeMap<&str, BTreeSet<&str>> = BTreeMap::new();
    for n in nodes {
        adj.insert(n.as_str(), BTreeSet::new());
    }
    // 只关心活跃事务之间的边（abort 后图已重建，正常情况下不会有悬挂边）。
    for e in edges {
        if nodes.contains(&e.from) && nodes.contains(&e.to) {
            adj.entry(e.from.as_str()).or_default().insert(e.to.as_str());
        }
    }

    // Tarjan，迭代实现以避免长环时递归深度问题（此处规模小，朴素实现即可）。
    let mut index: BTreeMap<&str, usize> = BTreeMap::new();
    let mut low: BTreeMap<&str, usize> = BTreeMap::new();
    let mut on_stack: BTreeSet<&str> = BTreeSet::new();
    let mut stack: Vec<&str> = Vec::new();
    let mut sccs: Vec<Vec<String>> = Vec::new();
    let mut counter = 0usize;

    // 递归 DFS（规模小；用显式栈模拟）。
    for root in adj.keys().copied().collect::<Vec<_>>() {
        if index.contains_key(root) {
            continue;
        }
        // 帧：(节点, 下一个邻接点下标)
        let mut frames: Vec<(&str, usize)> = vec![(root, 0)];
        index.insert(root, counter);
        low.insert(root, counter);
        counter += 1;
        stack.push(root);
        on_stack.insert(root);

        while let Some((u, next_i)) = frames.last_mut() {
            let neighbors: Vec<&str> = adj.get(*u).map(|s| s.iter().copied().collect()).unwrap_or_default();
            if *next_i < neighbors.len() {
                let v = neighbors[*next_i];
                *next_i += 1;
                if !index.contains_key(v) {
                    index.insert(v, counter);
                    low.insert(v, counter);
                    counter += 1;
                    stack.push(v);
                    on_stack.insert(v);
                    frames.push((v, 0));
                } else if on_stack.contains(v) {
                    let iv = *index.get(v).unwrap();
                    let lu = *low.get(*u).unwrap();
                    low.insert(*u, lu.min(iv));
                }
            } else {
                // 收尾 u。
                let lu = *low.get(u).unwrap();
                let iu = *index.get(u).unwrap();
                if lu == iu {
                    let mut comp = Vec::new();
                    loop {
                        let w = stack.pop().unwrap();
                        on_stack.remove(w);
                        comp.push(w.to_string());
                        if w == *u {
                            break;
                        }
                    }
                    if comp.len() >= 2 {
                        comp.sort();
                        sccs.push(comp);
                    }
                }
                frames.pop();
                if let Some((parent, _)) = frames.last() {
                    let lparent = *low.get(*parent).unwrap();
                    low.insert(*parent, lparent.min(lu));
                }
            }
        }
    }

    sccs.sort();
    sccs.dedup();
    sccs
}

#[cfg(test)]
mod tests {
    use super::*;

    fn edges(waits: &[EdgeView]) -> Vec<(String, String)> {
        waits.iter().map(|e| (e.from.clone(), e.to.clone())).collect()
    }

    #[test]
    fn interval_overlap_semantics() {
        assert!(Interval::new(0, 10).unwrap().overlaps(&Interval::new(5, 15).unwrap()));
        // 相邻、左闭右开 => 不重叠。
        assert!(!Interval::new(0, 10).unwrap().overlaps(&Interval::new(10, 20).unwrap()));
        assert!(!Interval::new(10, 20).unwrap().overlaps(&Interval::new(0, 10).unwrap()));
        assert!(Interval::new(0, 10).unwrap().overlaps(&Interval::new(9, 10).unwrap()));
        assert!(Interval::new(0, 10).unwrap().overlaps(&Interval::new(0, 1).unwrap()));
    }

    #[test]
    fn rejects_bad_interval_and_mode() {
        let mut e = Engine::new();
        e.begin("t1").unwrap();
        assert!(matches!(
            e.acquire("t1", "r", Mode::Shared, 5, 5),
            Err(EngineError::BadInterval { start: 5, end: 5 })
        ));
        assert!(Mode::parse("exclusive").is_ok());
        assert!(Mode::parse("weird").is_err());
    }

    #[test]
    fn shared_locks_compatible() {
        let mut e = Engine::new();
        e.begin("a").unwrap();
        e.begin("b").unwrap();
        assert!(e.acquire("a", "r", Mode::Shared, 0, 10).unwrap().granted);
        let r = e.acquire("b", "r", Mode::Shared, 2, 8).unwrap();
        assert!(r.granted);
    }

    #[test]
    fn adjacent_nonoverlapping_ranges_do_not_block_and_no_false_deadlock() {
        // 验收点：相邻不重叠区间——X 与 X 也互不阻塞，等待图必须为空（无误报）。
        let mut e = Engine::new();
        e.begin("a").unwrap();
        e.begin("b").unwrap();
        e.begin("c").unwrap();
        assert!(e.acquire("a", "r", Mode::Exclusive, 0, 10).unwrap().granted);
        assert!(e.acquire("b", "r", Mode::Exclusive, 10, 20).unwrap().granted);
        assert!(e.acquire("c", "r", Mode::Exclusive, 20, 30).unwrap().granted);
        assert!(e.build_waits().is_empty());
        // 真正重叠（哪怕只差 1）仍必须阻塞，防止把判断写反。
        assert!(e.acquire("b", "r", Mode::Exclusive, 9, 10).unwrap().queued);
    }

    #[test]
    fn two_transaction_cycle_detected_and_broken() {
        // 验收点：二环 A<->B，确定性牺牲者为 id 字典序最大的 B；
        // B 中止并释放全部锁后，等待中的 A 被兑现，可继续提交。
        let mut e = Engine::new();
        e.begin("A").unwrap();
        e.begin("B").unwrap();
        // A 持 r1，B 持 r2。
        e.acquire("A", "r1", Mode::Exclusive, 0, 10).unwrap();
        e.acquire("B", "r2", Mode::Exclusive, 0, 10).unwrap();
        // B 请求 r1：仅等待，无环。
        let r = e.acquire("B", "r1", Mode::Exclusive, 0, 10).unwrap();
        assert!(r.queued && r.deadlock.is_none());
        assert_eq!(edges(&e.build_waits()), vec![("B".into(), "A".into())]);
        // A 请求 r2：成环；{A,B} 中最大 id 为 B => B 被中止，A 的 r2 请求
        // 随 B 释放而当场兑现。
        let r = e.acquire("A", "r2", Mode::Exclusive, 0, 10).unwrap();
        assert!(r.granted);
        let dl = r.deadlock.clone().expect("must detect deadlock");
        assert_eq!(dl.cycles, vec![vec!["A".to_string(), "B".to_string()]]);
        assert_eq!(dl.victims[0], "B");
        assert!(e.is_active("A"));
        assert!(!e.is_active("B"));
        let view = e.resources_view();
        let r2 = view.iter().find(|v| v.resource == "r2").unwrap();
        assert_eq!(r2.held.len(), 1);
        assert_eq!(r2.held[0].txn, "A");
        // A 仍持有原来的 r1；B 对 r1 的等待请求随 B 中止而撤队。
        let r1 = view.iter().find(|v| v.resource == "r1").unwrap();
        assert_eq!(r1.held.len(), 1);
        assert_eq!(r1.held[0].txn, "A");
        assert!(r1.waiting.is_empty());
        assert!(r2.waiting.is_empty());
        // A 可以继续提交（释放全部锁）。
        e.commit("A").unwrap();
        assert!(e.build_waits().is_empty());
    }

    #[test]
    fn three_transaction_cycle_detected_and_broken() {
        // 验收点：三环 A->B->C->A。
        let mut e = Engine::new();
        for t in ["A", "B", "C"] {
            e.begin(t).unwrap();
        }
        e.acquire("A", "rA", Mode::Exclusive, 0, 10).unwrap();
        e.acquire("B", "rB", Mode::Exclusive, 0, 10).unwrap();
        e.acquire("C", "rC", Mode::Exclusive, 0, 10).unwrap();
        // A 等 B，B 等 C，尚不成环。
        assert!(e.acquire("A", "rB", Mode::Exclusive, 0, 10).unwrap().queued);
        assert!(e.acquire("B", "rC", Mode::Exclusive, 0, 10).unwrap().queued);
        // C 请求 rA：闭环，C 为最大 id => 牺牲 C。
        let r = e.acquire("C", "rA", Mode::Exclusive, 0, 10).unwrap();
        let dl = r.deadlock.expect("deadlock");
        assert_eq!(dl.cycles, vec![vec!["A".to_string(), "B".to_string(), "C".to_string()]]);
        assert_eq!(dl.victims[0], "C");
        // C 释放 rC：B 获得 rC；B 尚未请求别的，链条到此解开，A 仍等 rB。
        assert!(!e.is_active("C"));
        let view = e.resources_view();
        let rc = view.iter().find(|v| v.resource == "rC").unwrap();
        assert_eq!(rc.held.iter().map(|l| l.txn.as_str()).collect::<Vec<_>>(), vec!["B"]);
        // B 提交后 rB 释放，A 获得 rB。
        e.commit("B").unwrap();
        let view = e.resources_view();
        let rb = view.iter().find(|v| v.resource == "rB").unwrap();
        assert_eq!(rb.held[0].txn, "A");
        assert!(rb.waiting.is_empty());
        e.commit("A").unwrap();
    }

    #[test]
    fn fifo_queue_blocks_jumpers_and_graph_reflects_real_waits() {
        // B 在 r 上等待 A（X），此时 C 请求 S：即使后来 A 释放，
        // C 也不能越过 B。先验证等待图边的真实含义。
        let mut e = Engine::new();
        e.begin("A").unwrap();
        e.begin("B").unwrap();
        e.begin("C").unwrap();
        e.acquire("A", "r", Mode::Exclusive, 0, 10).unwrap();
        e.acquire("B", "r", Mode::Exclusive, 0, 10).unwrap(); // 等 A
        e.acquire("C", "r", Mode::Shared, 0, 10).unwrap(); // 前方有 B(X)
        let mut es = edges(&e.build_waits());
        es.sort();
        assert_eq!(
            es,
            vec![("B".into(), "A".into()), ("C".into(), "A".into()), ("C".into(), "B".into())]
        );
        // A 提交：B 获得 X；C 仍等 B（不能插队）。
        e.commit("A").unwrap();
        let v = e.resources_view();
        let r = &v[0];
        assert_eq!(r.held[0].txn, "B");
        assert_eq!(r.waiting.len(), 1);
        assert_eq!(r.waiting[0].txn, "C");
        // 同区间不重叠的另一把锁不受队列影响（不同区间、无冲突时也不越过冲突者）：
        // C 另外请求一个不与任何人重叠的区间，应立即授予。
        let ok = e.acquire("C", "r", Mode::Shared, 100, 200).unwrap();
        assert!(ok.granted);
    }

    #[test]
    fn nonoverlapping_request_passes_behind_unrelated_waiter() {
        // 等待者 B 占住队首等 [0,10)，新请求 C 针对 [50,60)：
        // 与持锁、与 B 都不冲突，应直接授予（队列只挡冲突者，不做无差别阻塞）。
        let mut e = Engine::new();
        e.begin("A").unwrap();
        e.begin("B").unwrap();
        e.begin("C").unwrap();
        e.acquire("A", "r", Mode::Exclusive, 0, 10).unwrap();
        assert!(e.acquire("B", "r", Mode::Exclusive, 0, 10).unwrap().queued);
        let r = e.acquire("C", "r", Mode::Exclusive, 50, 60).unwrap();
        assert!(r.granted);
        assert!(e.build_waits().iter().all(|x| x.from != "C"));
    }

    #[test]
    fn lock_upgrade_immediate() {
        // 验收点：锁升级。单事务 S -> X 立即成功。
        let mut e = Engine::new();
        e.begin("A").unwrap();
        e.begin("B").unwrap();
        e.acquire("A", "r", Mode::Shared, 0, 10).unwrap();
        let r = e.acquire("A", "r", Mode::Exclusive, 0, 10).unwrap();
        assert!(r.granted && r.upgraded);
        // 升级为 X 后，B 的 S 请求必须等待。
        assert!(e.acquire("B", "r", Mode::Shared, 0, 10).unwrap().queued);
    }

    #[test]
    fn lock_upgrade_blocks_and_can_deadlock() {
        // 升级请求参与真实等待图：
        // A、B 各持 S（重叠）；A 升级 X -> 等 B；B 升级 X -> 等 A => 二环。
        let mut e = Engine::new();
        e.begin("A").unwrap();
        e.begin("B").unwrap();
        e.acquire("A", "r", Mode::Shared, 0, 10).unwrap();
        e.acquire("B", "r", Mode::Shared, 0, 10).unwrap();
        assert!(e.acquire("A", "r", Mode::Exclusive, 0, 10).unwrap().queued);
        let r = e.acquire("B", "r", Mode::Exclusive, 0, 10).unwrap();
        let dl = r.deadlock.expect("upgrade deadlock must be detected");
        assert_eq!(dl.victims[0], "B");
        // B 中止后，A 的升级兑现为 X。
        let v = e.resources_view();
        let held: Vec<_> = v[0].held.iter().map(|l| (l.txn.as_str(), l.mode.as_str())).collect();
        assert_eq!(held, vec![("A", "exclusive")]);
        assert!(v[0].waiting.is_empty());
        e.commit("A").unwrap();
    }

    #[test]
    fn victim_releases_all_locks_across_resources() {
        // 牺牲者在多个资源上持锁/等待，中止后必须全部释放，其他事务继续。
        let mut e = Engine::new();
        e.begin("A").unwrap();
        e.begin("B").unwrap();
        e.begin("C").unwrap();
        e.acquire("A", "r1", Mode::Exclusive, 0, 10).unwrap();
        e.acquire("B", "r2", Mode::Exclusive, 0, 10).unwrap();
        // A 再拿一把 r3；C 在 r3 上等待 A（边 C->A）。
        e.acquire("A", "r3", Mode::Exclusive, 0, 10).unwrap();
        e.acquire("C", "r3", Mode::Exclusive, 0, 10).unwrap();
        // B 等 r1（边 B->A），随后 A 请求 r2 闭环 A<->B；最大 id B 牺牲。
        e.acquire("B", "r1", Mode::Exclusive, 0, 10).unwrap();
        let r = e.acquire("A", "r2", Mode::Exclusive, 0, 10).unwrap();
        assert_eq!(r.deadlock.unwrap().victims[0], "B");
        // B 被中止：释放 r2、撤回其 r1 等待请求；A 的 r2 请求当场兑现。
        // A 此刻持有 r1、r2、r3；C 仍在 r3 上等待 A（无环，属正常等待）。
        let v = e.resources_view();
        let get = |name: &str| v.iter().find(|x| x.resource == name).unwrap();
        assert_eq!(get("r1").held.len(), 1);
        assert_eq!(get("r1").held[0].txn, "A");
        assert_eq!(get("r2").held.len(), 1);
        assert_eq!(get("r2").held[0].txn, "A");
        assert_eq!(get("r3").held[0].txn, "A");
        assert_eq!(get("r3").waiting.len(), 1);
        assert_eq!(get("r3").waiting[0].txn, "C");
        // A 提交释放全部锁后，C 被推进获得 r3，图清空。
        e.commit("A").unwrap();
        let v = e.resources_view();
        let get = |name: &str| v.iter().find(|x| x.resource == name).unwrap();
        assert_eq!(get("r3").held.len(), 1);
        assert_eq!(get("r3").held[0].txn, "C");
        for rv in &v {
            assert!(rv.waiting.is_empty(), "{} still has waiters", rv.resource);
        }
        assert!(e.build_waits().is_empty());
    }

    #[test]
    fn aborted_txn_is_rejected_until_cleanup() {
        let mut e = Engine::new();
        e.begin("t1").unwrap();
        e.begin("t2").unwrap();
        e.acquire("t1", "r1", Mode::Exclusive, 0, 10).unwrap();
        e.acquire("t2", "r2", Mode::Exclusive, 0, 10).unwrap();
        // t1 等 t2 的 r2。
        e.acquire("t1", "r2", Mode::Exclusive, 0, 10).unwrap();
        // t2 闭环且为最大 id => t2 被中止。
        assert!(e.acquire("t2", "r1", Mode::Exclusive, 0, 10).unwrap().deadlock.is_some());
        // t2 已中止，不能再操作。
        assert!(matches!(e.acquire("t2", "r9", Mode::Shared, 0, 1), Err(EngineError::TxnAborted { .. })));
        assert!(matches!(e.commit("t2"), Err(EngineError::TxnAborted { .. })));
        // cleanup 后同 id 可重新开始。
        e.cleanup("t2");
        e.begin("t2").unwrap();
        assert!(e.acquire("t2", "r9", Mode::Shared, 0, 1).unwrap().granted);
    }

    #[test]
    fn two_cycles_sharing_only_caller_abort_repeatedly_until_acyclic() {
        // 多牺牲者场景：调用者 A 同时在两个二环 A<->B 与 A<->C 中（8 字形）。
        //
        // 布局（A 持有 r2 的 X；B、C 各持 r1 上一段 S，且都在等 r2）：
        //   A --X--> r2[0,10)
        //   B --S--> r1[0,6)，B --X等--> r2  => 边 B->A
        //   C --S--> r1[4,10)，C --X等--> r2 => 边 C->A（另有 FIFO 边 C->B）
        // 初始图无环。A 再请求 r1[0,10) 的 X：同时与 B、C 的 S 冲突，
        // 新增边 A->B、A->C，{A,B,C} 成一个 SCC（内含两个仅共享 A 的环）。
        // 最大 id 为 C => 先中止 C；残图中 A<->B 仍在 => 再中止 B；
        // 此后 A 的 X 请求被兑现。victims 必须是 [C, B]，且最终无环。
        let mut e = Engine::new();
        for t in ["A", "B", "C"] {
            e.begin(t).unwrap();
        }
        e.acquire("A", "r2", Mode::Exclusive, 0, 10).unwrap();
        e.acquire("B", "r1", Mode::Shared, 0, 6).unwrap();
        e.acquire("C", "r1", Mode::Shared, 4, 10).unwrap();
        assert!(e.acquire("B", "r2", Mode::Exclusive, 0, 10).unwrap().queued);
        assert!(e.acquire("C", "r2", Mode::Exclusive, 0, 10).unwrap().queued);
        assert!(e.build_waits().iter().all(|x| x.from != "A"));

        let r = e.acquire("A", "r1", Mode::Exclusive, 0, 10).unwrap();
        let dl = r.deadlock.expect("deadlock detected");
        assert_eq!(dl.cycles, vec![vec!["A".to_string(), "B".to_string(), "C".to_string()]]);
        assert_eq!(dl.victims, vec!["C".to_string(), "B".to_string()]);
        // 调用者 A 存活且最终获得 r1 的 X。
        assert!(r.granted);
        assert!(e.is_active("A"));
        assert!(!e.is_active("B"));
        assert!(!e.is_active("C"));
        assert!(e.build_waits().is_empty());
        let v = e.resources_view();
        let get = |name: &str| v.iter().find(|x| x.resource == name).unwrap();
        assert_eq!(get("r1").held.len(), 1);
        assert_eq!(get("r1").held[0].txn, "A");
        assert_eq!(get("r1").held[0].mode, "exclusive");
        assert_eq!(get("r2").held.len(), 1);
        assert_eq!(get("r2").held[0].txn, "A");
        e.commit("A").unwrap();
    }
}
