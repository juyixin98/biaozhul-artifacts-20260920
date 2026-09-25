//! 单机两阶段锁（2PL）管理器。
//!
//! 设计要点：
//! - 锁表按对象名哈希分桶（`buckets`），桶内为 `resource -> LockEntry`。
//! - 每个锁对象维护持有者集合与 FIFO 等待队列；共享锁（S）/排他锁（X），支持 S->X 升级。
//! - 等待图 `wait_for: waiter -> {holders 与队列中排在其前面的冲突等待者}`，
//!   邻接点用 BTreeSet 按事务 ID 升序，保证死锁检测与牺牲者选择是确定性的。
//!   只画"等待者->持有者"的边会漏掉经 FIFO 队列形成的环（例如 S 持有者 +
//!   排队中的 X 升级请求 + 其后排队的 S 请求），因此更早的冲突等待者也是阻塞源。
//! - 死锁检测：新等待边加入后，从等待者出发 DFS，发现回到起点的环即死锁。
//!   牺牲者 = 环上事务 ID 最大者（最年轻的事务），规则固定、可解释，
//!   解释字符串记录在 `deadlock_log` 中。
//! - 锁超时：等待超时的事务被整体中止并批量释放其全部锁（防止锁泄漏）。
//! - 等待图边数上限（默认 10000）：达到上限后拒绝新事务与新等待边，并累计告警指标。

use serde::Serialize;
use std::collections::{BTreeSet, HashMap, HashSet, VecDeque};
use std::hash::{Hash, Hasher};
use std::sync::Mutex;
use std::time::Duration;
use tokio::sync::oneshot;

pub type TxId = u64;

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum LockMode {
    Shared,
    Exclusive,
}

#[derive(Debug, Clone)]
pub struct Config {
    /// 锁表哈希桶数量
    pub num_buckets: usize,
    /// 等待图边数上限，超出拒绝新事务
    pub max_wait_edges: usize,
    /// 默认锁等待超时（毫秒）
    pub default_timeout_ms: u64,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            num_buckets: 64,
            max_wait_edges: 10_000,
            default_timeout_ms: 5_000,
        }
    }
}

// ---------------------------------------------------------------------------
// 错误类型
// ---------------------------------------------------------------------------

#[derive(Debug)]
pub enum AcquireError {
    /// 本事务被选为死锁牺牲者（附带可解释的环描述）
    Deadlock(String),
    /// 锁等待超时，事务已被中止（误杀候选：并非确认死锁）
    Timeout(String),
    /// 等待期间事务被外部中止
    Aborted(String),
    /// 事务不存在或已结束
    TxNotActive(String),
    /// 同一事务一次只允许一个未决锁请求
    AlreadyWaiting(String),
    /// 等待图边数达到上限
    GraphFull(String),
}

#[derive(Debug)]
pub enum BeginError {
    GraphFull(String),
}

#[derive(Debug)]
pub enum TxError {
    NotActive(String),
}

// ---------------------------------------------------------------------------
// 内部数据结构
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
enum AbortKind {
    Deadlock(String),
    Timeout(String),
    User,
    Committed,
}

impl AbortKind {
    fn describe(&self) -> String {
        match self {
            AbortKind::Deadlock(d) => format!("deadlock: {d}"),
            AbortKind::Timeout(d) => format!("timeout: {d}"),
            AbortKind::User => "aborted by user".to_string(),
            AbortKind::Committed => "transaction committed while waiting".to_string(),
        }
    }
}

enum WaitOutcome {
    Granted,
    Aborted(AbortKind),
}

struct Waiter {
    tx: TxId,
    mode: LockMode,
    is_upgrade: bool,
    notify: oneshot::Sender<WaitOutcome>,
}

#[derive(Default)]
struct LockEntry {
    /// 当前持有者：tx -> 持有模式
    holders: HashMap<TxId, LockMode>,
    /// FIFO 等待队列（升级请求排在队首）
    queue: VecDeque<Waiter>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "state", rename_all = "lowercase")]
pub enum TxStatus {
    Active,
    Committed,
    Aborted { reason: String },
}

type Outbox = Vec<(oneshot::Sender<WaitOutcome>, WaitOutcome)>;

#[derive(Default)]
struct Stats {
    locks_granted: u64,
    locks_waited: u64,
    commits: u64,
    deadlocks_detected: u64,
    /// 确认死锁而被中止的事务数
    deadlock_aborts: u64,
    /// 超时中止总数
    timeout_aborts: u64,
    /// 其中超时时刻自身仍在等待环上的个数（说明即时检测漏判、靠超时兜底）
    timeout_aborts_in_cycle: u64,
    user_aborts: u64,
    wait_chain_samples: u64,
    wait_chain_total: u64,
    wait_chain_max: usize,
    /// 等待图满导致的拒绝次数（告警指标）
    graph_full_rejections: u64,
    deadlock_log: VecDeque<String>,
}

const DEADLOCK_LOG_CAP: usize = 100;

struct Inner {
    next_tx: TxId,
    buckets: Vec<HashMap<String, LockEntry>>,
    /// 等待图：等待者 -> 其等待的持有者集合（BTreeSet 保证按事务 ID 排序遍历）
    wait_for: HashMap<TxId, BTreeSet<TxId>>,
    edge_count: usize,
    /// tx -> 持有的资源集合（用于提交/中止时批量释放，防止锁泄漏）
    tx_locks: HashMap<TxId, HashSet<String>>,
    /// tx -> 正在等待的资源（一个事务同一时刻至多一个未决请求）
    waiting_on: HashMap<TxId, String>,
    tx_state: HashMap<TxId, TxStatus>,
    stats: Stats,
    config: Config,
}

impl Inner {
    fn bucket_index(&self, resource: &str) -> usize {
        let mut h = std::collections::hash_map::DefaultHasher::new();
        resource.hash(&mut h);
        (h.finish() as usize) % self.buckets.len()
    }

    fn grant(&mut self, tx: TxId, resource: &str, mode: LockMode) {
        let idx = self.bucket_index(resource);
        let entry = self.buckets[idx].entry(resource.to_string()).or_default();
        entry.holders.insert(tx, mode);
        self.tx_locks
            .entry(tx)
            .or_default()
            .insert(resource.to_string());
        self.stats.locks_granted += 1;
    }

    /// 释放某个持有者对某资源的锁，并依次唤醒队列中可满足的等待者。
    fn release_holder(&mut self, tx: TxId, resource: &str, outbox: &mut Outbox) {
        let idx = self.bucket_index(resource);
        if let Some(entry) = self.buckets[idx].get_mut(resource) {
            entry.holders.remove(&tx);
        }
        self.process_queue(resource, outbox);
        // 空桶项及时清理，避免内存泄漏
        let empty = self.buckets[idx]
            .get(resource)
            .map(|e| e.holders.is_empty() && e.queue.is_empty())
            .unwrap_or(false);
        if empty {
            self.buckets[idx].remove(resource);
        }
    }

    /// 按 FIFO 处理等待队列：队首可满足则授予并继续，否则停止。
    fn process_queue(&mut self, resource: &str, outbox: &mut Outbox) {
        loop {
            let idx = self.bucket_index(resource);
            let Some(entry) = self.buckets[idx].get_mut(resource) else {
                return;
            };
            let Some(front) = entry.queue.front() else {
                break;
            };
            let (ftx, fmode, fupgrade) = (front.tx, front.mode, front.is_upgrade);
            let can = match (fupgrade, fmode) {
                // 共享锁：当前无排他持有者即可授予
                (_, LockMode::Shared) => !entry.holders.values().any(|m| *m == LockMode::Exclusive),
                // 升级请求：除自己外无其他持有者
                (true, LockMode::Exclusive) => {
                    entry.holders.is_empty()
                        || (entry.holders.len() == 1 && entry.holders.contains_key(&ftx))
                }
                // 普通排他锁：无任何持有者
                (false, LockMode::Exclusive) => entry.holders.is_empty(),
            };
            if !can {
                break;
            }
            let w = entry.queue.pop_front().expect("front checked");
            entry.holders.insert(ftx, fmode);
            self.tx_locks.entry(ftx).or_default().insert(resource.to_string());
            self.waiting_on.remove(&ftx);
            // 被唤醒者不再是等待者，清除其出边
            if let Some(old) = self.wait_for.remove(&ftx) {
                self.edge_count -= old.len();
            }
            self.stats.locks_granted += 1;
            outbox.push((w.notify, WaitOutcome::Granted));
        }
        self.rebuild_edges_for(resource);
    }

    /// 持有者集合或队列变化后，重建该资源所有等待者的等待边（保持等待图精确）。
    ///
    /// 等待者 W 的阻塞源有两类：
    /// 1. 与 W 请求模式冲突的当前持有者；
    /// 2. FIFO 队列中排在 W 前面、且其请求模式与 W 冲突的等待者
    ///    （队首授予前 W 不可能越过它获得锁）。
    ///
    ///    缺少第 2 类边会漏掉只经队列闭合的死锁环。
    fn rebuild_edges_for(&mut self, resource: &str) {
        #[inline]
        fn conflicts(want: LockMode, other: LockMode) -> bool {
            want == LockMode::Exclusive || other == LockMode::Exclusive
        }
        let idx = self.bucket_index(resource);
        let Some(entry) = self.buckets[idx].get(resource) else {
            return;
        };
        let mut desired: Vec<(TxId, BTreeSet<TxId>)> = Vec::with_capacity(entry.queue.len());
        for (pos, w) in entry.queue.iter().enumerate() {
            let mut set = BTreeSet::new();
            for (h, m) in &entry.holders {
                if *h != w.tx && conflicts(w.mode, *m) {
                    set.insert(*h);
                }
            }
            for earlier in entry.queue.iter().take(pos) {
                if earlier.tx != w.tx && conflicts(w.mode, earlier.mode) {
                    set.insert(earlier.tx);
                }
            }
            desired.push((w.tx, set));
        }
        for (tx, set) in desired {
            if let Some(old) = self.wait_for.remove(&tx) {
                self.edge_count -= old.len();
            }
            if !set.is_empty() {
                self.edge_count += set.len();
                self.wait_for.insert(tx, set);
            }
        }
    }

    /// 从 `start` 出发沿等待边做确定性 DFS（邻接按事务 ID 升序）。
    /// 返回 (环路径, 探测到的最长等待链长度)。环路径首尾均为 start。
    fn find_cycle(&self, start: TxId) -> (Option<Vec<TxId>>, usize) {
        let mut max_depth = 0usize;
        let mut path: Vec<TxId> = vec![start];
        let mut on_path: HashSet<TxId> = HashSet::from([start]);
        let mut done: HashSet<TxId> = HashSet::new();
        let mut iters: Vec<std::collections::btree_set::Iter<'_, TxId>> = Vec::new();
        match self.wait_for.get(&start) {
            Some(s) => iters.push(s.iter()),
            None => return (None, 0),
        }
        while let Some(top) = iters.last_mut() {
            match top.next() {
                Some(&next) => {
                    if next == start {
                        let mut cyc = path.clone();
                        cyc.push(start);
                        return (Some(cyc), max_depth.max(path.len()));
                    }
                    if on_path.contains(&next) || done.contains(&next) {
                        continue;
                    }
                    on_path.insert(next);
                    path.push(next);
                    max_depth = max_depth.max(path.len() - 1);
                    match self.wait_for.get(&next) {
                        Some(s) => iters.push(s.iter()),
                        None => {
                            on_path.remove(&next);
                            path.pop();
                            done.insert(next);
                        }
                    }
                }
                None => {
                    iters.pop();
                    if let Some(&last) = path.last() {
                        on_path.remove(&last);
                        done.insert(last);
                    }
                    path.pop();
                }
            }
        }
        (None, max_depth)
    }

    /// 中止事务：移除其等待请求、批量释放其全部锁、清除相关等待边。
    /// 所有需要唤醒的对端通过 outbox 在锁外通知。
    fn abort_tx(&mut self, tx: TxId, kind: AbortKind, outbox: &mut Outbox) {
        self.tx_state.insert(
            tx,
            TxStatus::Aborted {
                reason: kind.describe(),
            },
        );
        // 1. 移除未决等待请求；该等待者可能正挡在队首，移除后须 pump 队列，
        //    否则后续可满足的等待者会被无谓阻塞（甚至被误判进死锁环）。
        if let Some(res) = self.waiting_on.remove(&tx) {
            let idx = self.bucket_index(&res);
            if let Some(entry) = self.buckets[idx].get_mut(&res) {
                if let Some(pos) = entry.queue.iter().position(|w| w.tx == tx) {
                    let w = entry.queue.remove(pos).expect("position checked");
                    outbox.push((w.notify, WaitOutcome::Aborted(kind.clone())));
                }
            }
            self.process_queue(&res, outbox);
        }
        // 2. 批量释放持有的全部锁
        let resources: Vec<String> = self
            .tx_locks
            .remove(&tx)
            .unwrap_or_default()
            .into_iter()
            .collect();
        for res in resources {
            self.release_holder(tx, &res, outbox);
        }
        // 3. 清除等待图中与其相关的边（出边 + 入边，兜底）
        if let Some(out) = self.wait_for.remove(&tx) {
            self.edge_count -= out.len();
        }
        let mut emptied = Vec::new();
        for (w, set) in self.wait_for.iter_mut() {
            if set.remove(&tx) {
                self.edge_count -= 1;
            }
            if set.is_empty() {
                emptied.push(*w);
            }
        }
        for w in emptied {
            self.wait_for.remove(&w);
        }
    }

    fn check_active(&self, tx: TxId) -> Result<(), AcquireError> {
        match self.tx_state.get(&tx) {
            Some(TxStatus::Active) => Ok(()),
            Some(TxStatus::Committed) => Err(AcquireError::TxNotActive(format!(
                "tx {tx} already committed"
            ))),
            Some(TxStatus::Aborted { reason }) => Err(AcquireError::TxNotActive(format!(
                "tx {tx} already aborted: {reason}"
            ))),
            None => Err(AcquireError::TxNotActive(format!("tx {tx} not found"))),
        }
    }
}

/// 两种请求/持有模式是否冲突（S 仅与 X 冲突，X 与一切冲突）。
fn conflicts_with(want: LockMode, other: LockMode) -> bool {
    want == LockMode::Exclusive || other == LockMode::Exclusive
}

/// 判断新请求是否可立即授予（不进入等待队列）。
fn can_grant_now(entry: &LockEntry, tx: TxId, mode: LockMode) -> bool {
    let held = entry.holders.get(&tx).copied();
    match (held, mode) {
        (Some(LockMode::Exclusive), _) => true,
        (Some(LockMode::Shared), LockMode::Shared) => true,
        // 升级：自己是唯一持有者可就地升级
        (Some(LockMode::Shared), LockMode::Exclusive) => entry.holders.len() == 1,
        (None, LockMode::Shared) => {
            entry.holders.values().all(|m| *m == LockMode::Shared) && entry.queue.is_empty()
        }
        (None, LockMode::Exclusive) => entry.holders.is_empty() && entry.queue.is_empty(),
    }
}

// ---------------------------------------------------------------------------
// 对外接口
// ---------------------------------------------------------------------------

pub struct LockManager {
    inner: Mutex<Inner>,
}

impl LockManager {
    pub fn new(config: Config) -> Self {
        let buckets = (0..config.num_buckets.max(1))
            .map(|_| HashMap::new())
            .collect();
        Self {
            inner: Mutex::new(Inner {
                next_tx: 0,
                buckets,
                wait_for: HashMap::new(),
                edge_count: 0,
                tx_locks: HashMap::new(),
                waiting_on: HashMap::new(),
                tx_state: HashMap::new(),
                stats: Stats::default(),
                config,
            }),
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// 开启新事务。等待图边数达到上限时拒绝（告警指标 +1）。
    pub fn begin(&self) -> Result<TxId, BeginError> {
        let mut inner = self.lock();
        if inner.edge_count >= inner.config.max_wait_edges {
            inner.stats.graph_full_rejections += 1;
            let msg = format!(
                "wait-for graph full ({} edges >= {}), rejecting new transaction",
                inner.edge_count, inner.config.max_wait_edges
            );
            eprintln!("[ALERT] {msg}");
            return Err(BeginError::GraphFull(msg));
        }
        inner.next_tx += 1;
        let id = inner.next_tx;
        inner.tx_state.insert(id, TxStatus::Active);
        Ok(id)
    }

    /// 申请锁。立即可满足则同步授予；否则进入等待队列，
    /// 异步等待 授予 / 死锁中止 / 超时中止。
    pub async fn acquire(
        &self,
        tx: TxId,
        resource: &str,
        mode: LockMode,
        timeout: Option<Duration>,
    ) -> Result<(), AcquireError> {
        let timeout_dur =
            timeout.unwrap_or_else(|| Duration::from_millis(self.lock().config.default_timeout_ms));
        let resource = resource.to_string();

            let rx = {
                let mut inner = self.lock();
                inner.check_active(tx)?;
                if inner.waiting_on.contains_key(&tx) {
                    return Err(AcquireError::AlreadyWaiting(format!(
                        "tx {tx} already has a pending lock request"
                    )));
                }
                let rx = {
                    let idx = inner.bucket_index(&resource);
                    let (edge_count, max_edges) =
                        (inner.edge_count, inner.config.max_wait_edges);
                    let entry = inner.buckets[idx].entry(resource.clone()).or_default();
                    let held = entry.holders.get(&tx).copied();
                    let is_upgrade = held == Some(LockMode::Shared) && mode == LockMode::Exclusive;
                    if can_grant_now(entry, tx, mode) {
                        inner.grant(tx, &resource, mode);
                        return Ok(());
                    }
                    // 等待图容量预检：新等待者的出边 = 冲突持有者 +
                    // 队列中排在前面的冲突等待者（与 rebuild_edges_for 的口径一致）。
                    let needed = {
                        let mut n = entry
                            .holders
                            .iter()
                            .filter(|(h, m)| **h != tx && conflicts_with(mode, **m))
                            .count();
                        n += entry
                            .queue
                            .iter()
                            .filter(|w| w.tx != tx && conflicts_with(mode, w.mode))
                            .count();
                        n
                    };
                    if edge_count + needed > max_edges {
                        inner.stats.graph_full_rejections += 1;
                        eprintln!(
                            "[ALERT] wait-for graph full: {edge_count} + {needed} edges > {max_edges}; rejecting lock request by tx {tx} on '{resource}'"
                        );
                        return Err(AcquireError::GraphFull(format!(
                            "wait-for graph full ({edge_count} + {needed} edges > {max_edges}), rejecting lock request"
                        )));
                    }
                    // 入队（升级请求排队首）
                    let (snd, rx) = oneshot::channel();
                    let waiter = Waiter {
                        tx,
                        mode,
                        is_upgrade,
                        notify: snd,
                    };
                    if is_upgrade {
                        entry.queue.push_front(waiter);
                    } else {
                        entry.queue.push_back(waiter);
                    }
                    rx
                };
            inner.waiting_on.insert(tx, resource.clone());
            inner.rebuild_edges_for(&resource);
            inner.stats.locks_waited += 1;
            // 死锁检测（每次新增等待边后触发）
            let (cycle, depth) = inner.find_cycle(tx);
            inner.stats.wait_chain_samples += 1;
            inner.stats.wait_chain_total += depth as u64;
            inner.stats.wait_chain_max = inner.stats.wait_chain_max.max(depth);
            let mut outbox: Outbox = Vec::new();
            if let Some(cycle) = cycle {
                // 确定性牺牲者选择：环上事务 ID 最大者（最年轻的事务）
                let victim = *cycle.iter().max().expect("non-empty cycle");
                let path = cycle
                    .iter()
                    .map(|t| format!("T{t}"))
                    .collect::<Vec<_>>()
                    .join(" -> ");
                let explanation = format!(
                    "cycle {path}; victim T{victim} (largest txid = youngest transaction in cycle)"
                );
                inner.stats.deadlocks_detected += 1;
                inner.stats.deadlock_aborts += 1;
                if inner.stats.deadlock_log.len() >= DEADLOCK_LOG_CAP {
                    inner.stats.deadlock_log.pop_front();
                }
                inner.stats.deadlock_log.push_back(explanation.clone());
                inner.abort_tx(victim, AbortKind::Deadlock(explanation), &mut outbox);
            }
            drop(inner);
            for (s, o) in outbox {
                let _ = s.send(o);
            }
            rx
        };

        match tokio::time::timeout(timeout_dur, rx).await {
            Ok(Ok(WaitOutcome::Granted)) => Ok(()),
            Ok(Ok(WaitOutcome::Aborted(kind))) => match kind {
                AbortKind::Deadlock(d) => Err(AcquireError::Deadlock(d)),
                AbortKind::Timeout(d) => Err(AcquireError::Timeout(d)),
                other => Err(AcquireError::Aborted(other.describe())),
            },
            Ok(Err(_closed)) => Err(AcquireError::Aborted(
                "lock manager dropped waiter unexpectedly".into(),
            )),
            Err(_elapsed) => {
                // 锁超时：整体中止事务并批量释放其锁，防止锁泄漏。
                // 中止前先做一次死锁检测用于统计分类：若该事务此刻仍在环上，
                // 说明即时检测漏判而靠超时兜底（正常不应发生）；否则计为误杀。
                let mut outbox: Outbox = Vec::new();
                {
                    let mut inner = self.lock();
                    if matches!(inner.tx_state.get(&tx), Some(TxStatus::Active)) {
                        let in_cycle = inner.find_cycle(tx).0.is_some();
                        inner.stats.timeout_aborts += 1;
                        if in_cycle {
                            inner.stats.timeout_aborts_in_cycle += 1;
                            eprintln!(
                                "[WARN] tx {tx} timed out while still on a wait-for cycle (immediate detector missed it)"
                            );
                        }
                        inner.abort_tx(
                            tx,
                            AbortKind::Timeout(format!(
                                "lock wait on '{resource}' exceeded {}ms",
                                timeout_dur.as_millis()
                            )),
                            &mut outbox,
                        );
                    }
                }
                for (s, o) in outbox {
                    let _ = s.send(o);
                }
                Err(AcquireError::Timeout(format!(
                    "lock wait on '{resource}' exceeded {}ms; tx {tx} aborted",
                    timeout_dur.as_millis()
                )))
            }
        }
    }

    /// 提交：批量释放全部锁。
    pub fn commit(&self, tx: TxId) -> Result<(), TxError> {
        let mut outbox: Outbox = Vec::new();
        {
            let mut inner = self.lock();
            match inner.tx_state.get(&tx) {
                Some(TxStatus::Active) => {}
                _ => {
                    return Err(TxError::NotActive(format!(
                        "tx {tx} is not active, cannot commit"
                    )))
                }
            }
            inner.stats.commits += 1;
            // 提交等价于“释放全部锁 + 标记提交”
            if let Some(res) = inner.waiting_on.remove(&tx) {
                let idx = inner.bucket_index(&res);
                if let Some(entry) = inner.buckets[idx].get_mut(&res) {
                    if let Some(pos) = entry.queue.iter().position(|w| w.tx == tx) {
                        let w = entry.queue.remove(pos).expect("position checked");
                        outbox.push((w.notify, WaitOutcome::Aborted(AbortKind::Committed)));
                    }
                }
                // 与 abort 同理：移除等待者后 pump 队列
                inner.process_queue(&res, &mut outbox);
            }
            let resources: Vec<String> = inner
                .tx_locks
                .remove(&tx)
                .unwrap_or_default()
                .into_iter()
                .collect();
            for res in resources {
                inner.release_holder(tx, &res, &mut outbox);
            }
            if let Some(out) = inner.wait_for.remove(&tx) {
                inner.edge_count -= out.len();
            }
            inner.tx_state.insert(tx, TxStatus::Committed);
        }
        for (s, o) in outbox {
            let _ = s.send(o);
        }
        Ok(())
    }

    /// 用户主动中止：批量释放全部锁。
    pub fn abort(&self, tx: TxId) -> Result<(), TxError> {
        let mut outbox: Outbox = Vec::new();
        {
            let mut inner = self.lock();
            match inner.tx_state.get(&tx) {
                Some(TxStatus::Active) => {}
                _ => {
                    return Err(TxError::NotActive(format!(
                        "tx {tx} is not active, cannot abort"
                    )))
                }
            }
            inner.stats.user_aborts += 1;
            inner.abort_tx(tx, AbortKind::User, &mut outbox);
        }
        for (s, o) in outbox {
            let _ = s.send(o);
        }
        Ok(())
    }

    pub fn tx_status(&self, tx: TxId) -> Option<TxStatus> {
        self.lock().tx_state.get(&tx).cloned()
    }

    /// 当前等待图边集（按 from/to 排序，确定性输出）。
    pub fn graph_edges(&self) -> Vec<Edge> {
        let inner = self.lock();
        let mut edges = Vec::new();
        for (from, set) in &inner.wait_for {
            for to in set {
                edges.push(Edge { from: *from, to: *to });
            }
        }
        edges.sort_by_key(|e| (e.from, e.to));
        edges
    }

    pub fn metrics(&self) -> Metrics {
        let inner = self.lock();
        let current_holders: usize = inner
            .buckets
            .iter()
            .flat_map(|b| b.values())
            .map(|e| e.holders.len())
            .sum();
        let current_waiters: usize = inner
            .buckets
            .iter()
            .flat_map(|b| b.values())
            .map(|e| e.queue.len())
            .sum();
        let active_txs = inner
            .tx_state
            .values()
            .filter(|s| matches!(s, TxStatus::Active))
            .count();
        let s = &inner.stats;
        Metrics {
            locks_granted: s.locks_granted,
            locks_waited: s.locks_waited,
            commits: s.commits,
            deadlocks_detected: s.deadlocks_detected,
            deadlock_aborts: s.deadlock_aborts,
            timeout_aborts: s.timeout_aborts,
            // 误杀数：超时中止且当时不在任何等待环上（未经死锁确认即被中止）
            timeout_aborts_false_kill: s.timeout_aborts - s.timeout_aborts_in_cycle,
            timeout_aborts_in_cycle: s.timeout_aborts_in_cycle,
            user_aborts: s.user_aborts,
            wait_chain_samples: s.wait_chain_samples,
            wait_chain_avg: if s.wait_chain_samples > 0 {
                s.wait_chain_total as f64 / s.wait_chain_samples as f64
            } else {
                0.0
            },
            wait_chain_max: s.wait_chain_max,
            wait_graph_edges: inner.edge_count,
            wait_graph_max_edges: inner.config.max_wait_edges,
            wait_graph_full_rejections: s.graph_full_rejections,
            current_holders,
            current_waiters,
            active_txs,
            deadlock_log: s.deadlock_log.iter().cloned().collect(),
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize)]
pub struct Edge {
    pub from: TxId,
    pub to: TxId,
}

#[derive(Debug, Clone, Serialize)]
pub struct Metrics {
    pub locks_granted: u64,
    pub locks_waited: u64,
    pub commits: u64,
    pub deadlocks_detected: u64,
    pub deadlock_aborts: u64,
    /// 超时中止总数
    pub timeout_aborts: u64,
    /// 误杀数：超时中止且超时时刻不在等待环上（未经死锁确认即被中止）
    pub timeout_aborts_false_kill: u64,
    /// 超时兜底：超时时刻仍在环上（即时检测漏判的告警指标，正常应为 0）
    pub timeout_aborts_in_cycle: u64,
    pub user_aborts: u64,
    pub wait_chain_samples: u64,
    pub wait_chain_avg: f64,
    pub wait_chain_max: usize,
    pub wait_graph_edges: usize,
    pub wait_graph_max_edges: usize,
    /// 等待图满告警：拒绝新事务/新等待的次数
    pub wait_graph_full_rejections: u64,
    pub current_holders: usize,
    pub current_waiters: usize,
    pub active_txs: usize,
    pub deadlock_log: Vec<String>,
}

// ---------------------------------------------------------------------------
// 单元测试
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    fn mgr() -> Arc<LockManager> {
        Arc::new(LockManager::new(Config::default()))
    }

    async fn wait_edge(m: &LockManager, from: TxId, to: TxId) {
        for _ in 0..200 {
            if m.graph_edges().iter().any(|e| e.from == from && e.to == to) {
                return;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        panic!("edge {from}->{to} never appeared");
    }

    #[tokio::test]
    async fn shared_locks_are_compatible() {
        let m = mgr();
        let t1 = m.begin().unwrap();
        let t2 = m.begin().unwrap();
        m.acquire(t1, "A", LockMode::Shared, None).await.unwrap();
        m.acquire(t2, "A", LockMode::Shared, None).await.unwrap();
        m.commit(t1).unwrap();
        m.commit(t2).unwrap();
        let met = m.metrics();
        assert_eq!(met.current_holders, 0);
        assert_eq!(met.current_waiters, 0);
    }

    #[tokio::test]
    async fn exclusive_blocks_then_granted_after_commit() {
        let m = mgr();
        let t1 = m.begin().unwrap();
        let t2 = m.begin().unwrap();
        m.acquire(t1, "A", LockMode::Exclusive, None).await.unwrap();
        let m2 = m.clone();
        let h = tokio::spawn(async move { m2.acquire(t2, "A", LockMode::Exclusive, None).await });
        wait_edge(&m, t2, t1).await;
        m.commit(t1).unwrap();
        h.await.unwrap().unwrap();
        m.commit(t2).unwrap();
        assert_eq!(m.metrics().current_holders, 0);
    }

    #[tokio::test]
    async fn upgrade_in_place_when_sole_holder() {
        let m = mgr();
        let t1 = m.begin().unwrap();
        m.acquire(t1, "A", LockMode::Shared, None).await.unwrap();
        m.acquire(t1, "A", LockMode::Exclusive, None).await.unwrap();
        m.commit(t1).unwrap();
    }

    #[tokio::test]
    async fn two_tx_cycle_aborts_exactly_one() {
        let m = mgr();
        let t1 = m.begin().unwrap();
        let t2 = m.begin().unwrap();
        m.acquire(t1, "A", LockMode::Exclusive, None).await.unwrap();
        m.acquire(t2, "B", LockMode::Exclusive, None).await.unwrap();
        let m2 = m.clone();
        let h = tokio::spawn(async move { m2.acquire(t1, "B", LockMode::Exclusive, None).await });
        wait_edge(&m, t1, t2).await;
        let r2 = m.acquire(t2, "A", LockMode::Exclusive, None).await;
        // 牺牲者必须是 ID 较大者 T2，且只有 T2 被中止
        match r2 {
            Err(AcquireError::Deadlock(d)) => {
                assert!(d.contains(&format!("victim T{t2}")), "explanation: {d}");
            }
            other => panic!("expected deadlock for t2, got {other:?}"),
        }
        h.await.unwrap().unwrap(); // T1 获得 B
        m.commit(t1).unwrap();
        let met = m.metrics();
        assert_eq!(met.deadlocks_detected, 1);
        assert_eq!(met.deadlock_aborts, 1);
        assert_eq!(met.current_holders, 0);
        assert_eq!(met.wait_graph_edges, 0);
    }

    #[tokio::test]
    async fn upgrade_deadlock_detected() {
        let m = mgr();
        let t1 = m.begin().unwrap();
        let t2 = m.begin().unwrap();
        m.acquire(t1, "A", LockMode::Shared, None).await.unwrap();
        m.acquire(t2, "A", LockMode::Shared, None).await.unwrap();
        let m2 = m.clone();
        let h = tokio::spawn(async move { m2.acquire(t1, "A", LockMode::Exclusive, None).await });
        wait_edge(&m, t1, t2).await;
        let r2 = m.acquire(t2, "A", LockMode::Exclusive, None).await;
        assert!(matches!(r2, Err(AcquireError::Deadlock(_))));
        h.await.unwrap().unwrap();
        m.commit(t1).unwrap();
    }

    #[tokio::test]
    async fn timeout_aborts_and_releases_without_leak() {
        let m = mgr();
        let t1 = m.begin().unwrap();
        let t2 = m.begin().unwrap();
        m.acquire(t1, "A", LockMode::Exclusive, None).await.unwrap();
        let r = m
            .acquire(t2, "A", LockMode::Exclusive, Some(Duration::from_millis(100)))
            .await;
        assert!(matches!(r, Err(AcquireError::Timeout(_))));
        // 超时后 T2 已中止；T1 提交后锁表应清空
        m.commit(t1).unwrap();
        let met = m.metrics();
        assert_eq!(met.timeout_aborts_false_kill, 1);
        assert_eq!(met.current_holders, 0);
        assert_eq!(met.current_waiters, 0);
        assert_eq!(met.wait_graph_edges, 0);
    }

    /// 经 FIFO 队列闭合的环（修复前只画 holder 边会漏检，只能等超时误杀）：
    /// T1 持 S(A)；T3 持 X(B)；
    /// T2 排队等 X(A)：T2 -> T1；
    /// T3 在 T2 之后排队等 S(A)：与持有者 T1 兼容，仅被队首 T2 阻塞：T3 -> T2；
    /// T1 再等 X(B)：T1 -> T3，环 T1 -> T3 -> T2 -> T1 闭合。
    #[tokio::test]
    async fn cycle_closed_through_waiter_queue_is_detected() {
        let m = mgr();
        let t1 = m.begin().unwrap();
        let t2 = m.begin().unwrap();
        let t3 = m.begin().unwrap();
        m.acquire(t1, "A", LockMode::Shared, None).await.unwrap();
        m.acquire(t3, "B", LockMode::Exclusive, None).await.unwrap();

        let m2 = m.clone();
        let h2 = tokio::spawn(async move {
            m2.acquire(t2, "A", LockMode::Exclusive, None).await
        });
        wait_edge(&m, t2, t1).await;

        let m3 = m.clone();
        let h3 = tokio::spawn(async move {
            m3.acquire(t3, "A", LockMode::Shared, None).await
        });
        // 关键队列边：T3 的 S 与持有者 T1 的 S 兼容，只被前面的 X 等待者 T2 阻塞
        wait_edge(&m, t3, t2).await;

        // T1 等 B 使环闭合；牺牲者为最大 ID 的 T3，T1 自身随后被授予
        m.acquire(t1, "B", LockMode::Exclusive, None)
            .await
            .unwrap();

        match h3.await.unwrap() {
            Err(AcquireError::Deadlock(d)) => {
                assert!(d.contains(&format!("victim T{t3}")), "{d}");
            }
            other => panic!("T3 must be the deadlock victim: {other:?}"),
        }
        // T2 仍在等 X(A)；T1 提交释放 S(A) 后 T2 获得
        m.commit(t1).unwrap();
        h2.await.unwrap().unwrap();
        m.commit(t2).unwrap();

        let met = m.metrics();
        assert_eq!(met.deadlocks_detected, 1);
        assert_eq!(met.deadlock_aborts, 1);
        assert_eq!(met.timeout_aborts_false_kill, 0, "must not rely on timeout");
        assert_eq!(met.current_holders, 0);
        assert_eq!(met.current_waiters, 0);
        assert_eq!(met.wait_graph_edges, 0);
    }

    #[tokio::test]
    async fn graph_cap_rejects_new_transactions() {
        let m = Arc::new(LockManager::new(Config {
            max_wait_edges: 1,
            ..Config::default()
        }));
        let t1 = m.begin().unwrap();
        let t2 = m.begin().unwrap();
        m.acquire(t1, "A", LockMode::Exclusive, None).await.unwrap();
        let m2 = m.clone();
        let _h = tokio::spawn(async move { m2.acquire(t2, "A", LockMode::Exclusive, None).await });
        wait_edge(&m, t2, t1).await;
        // 边数已达上限：新事务被拒绝
        assert!(matches!(m.begin(), Err(BeginError::GraphFull(_))));
        // 新等待边也被拒绝
        let r = m.acquire(t1, "A", LockMode::Exclusive, None).await;
        assert!(r.is_ok()); // 已持有，重入直接成功
        let met = m.metrics();
        assert!(met.wait_graph_full_rejections >= 1);
    }
}
