//! # 离线 IPv4 分片重组引擎
//!
//! 纯内存、确定性时间（调用方传入毫秒时间戳），不接触原始套接字，
//! 不需要任何系统权限。
//!
//! ## 分组键
//!
//! 每个未完成数据报按 **(源地址, 目的地址, 协议号, 标识 ID)**
//! 四元组（[`Key`]）独立重组——这是 RFC 791 的分片归属粒度。
//!
//! ## 重叠片策略（显式、可测）
//!
//! 当前仅实现 [`OverlapPolicy::Reject`]：
//!
//! * **完全重复片**（偏移、长度、逐字节内容都相同）→ 幂等接受，
//!   不重复计费，仅刷新寿命；
//! * 任何其它字节范围交叠（即使交叠区内容相同）→ 返回
//!   [`ReassemblyError::OverlapConflict`]，并**整体丢弃该数据报**
//!   （已缓存的同组片全部释放）。这是刻意保守、抗重叠注入的策略。
//!
//! 检测与处理均由 [`Reassembler::add_packet`] /
//! [`add_fragment`](Reassembler::add_fragment) 完成。
//!
//! ## 寿命（TTL）
//!
//! 每收到一片就把该组的过期时间刷新为 `now + fragment_ttl_ms`。
//! 惰性清理：每次新增前、以及显式调用
//! [`purge_expired`](Reassembler::purge_expired) 时移除过期组。
//!
//! ## 内存预算
//!
//! * 总预算 `total_memory_budget`：所有组缓存的分片字节之和的硬上限；
//!   超限时新片被拒绝（[`ReassemblyError::BudgetExceeded`]），
//!   引擎**不会**为了腾地方去淘汰其它数据报；
//! * 单数据报上限 `max_datagram_payload`：单组缓存超过该值返回
//!   [`ReassemblyError::OversizedDatagram`]（IPv4 最大载荷 65535）。
//!
//! ## ID 重用
//!
//! 四元组在旧数据报未完成时被新数据报重用：若新首片内容与已缓存
//! 首片不同，或新片越过了旧组已知总长度，则判定为 ID 重用——
//! 旧组被整体丢弃，新组从本片重新开始（见测试 `id_reuse_replaces_old`）。

use std::collections::HashMap;
use std::net::Ipv4Addr;
use std::time::Duration;

use crate::ipv4::{self, Ipv4Error, Ipv4Header};

/// 重叠片处置策略。
///
/// 当前版本只支持 [`Reject`](OverlapPolicy::Reject)；保留枚举以便
/// 将来扩展（例如 RFC 5722 式“后到者覆盖”策略）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
#[non_exhaustive]
pub enum OverlapPolicy {
    /// 拒绝任何非完全重复的交叠片，整体丢弃该数据报。
    #[default]
    Reject,
}

/// 引擎配置。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Config {
    /// 单片组寿命：自最后一片到达后多久过期（毫秒）。
    pub fragment_ttl_ms: u64,
    /// 全引擎分片缓存总字节预算。
    pub total_memory_budget: usize,
    /// 单个数据报重组结果的最大载荷字节数。
    pub max_datagram_payload: usize,
}

impl Default for Config {
    /// RFC 2460 风格默认值：30 秒寿命、1 MiB 总预算、65535 单包上限。
    fn default() -> Self {
        Config {
            fragment_ttl_ms: 30_000,
            total_memory_budget: 1 << 20,
            max_datagram_payload: 65_535,
        }
    }
}

impl Config {
    /// 寿命（[`Duration`] 形式）。
    pub fn ttl(&self) -> Duration {
        Duration::from_millis(self.fragment_ttl_ms)
    }
}

/// 分组键：(源, 目的, 协议号, IP ID)。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct Key {
    pub src: Ipv4Addr,
    pub dst: Ipv4Addr,
    pub protocol: u8,
    pub identification: u16,
}

impl std::fmt::Display for Key {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "{} -> {} proto={} id={:#06x}",
            self.src, self.dst, self.protocol, self.identification
        )
    }
}

/// 重组完成的数据报。
#[derive(Debug, Clone)]
pub struct ReassembledDatagram {
    pub key: Key,
    /// 重组后完整载荷，已与原始载荷逐字节对照。
    pub payload: Vec<u8>,
    /// 首片到达时间（引擎时钟，毫秒）。
    pub first_arrival_ms: u64,
    /// 促成重组的最后一片到达时间。
    pub completed_at_ms: u64,
    /// 参与重组的不同片数（重复片不计）。
    pub fragment_count: usize,
}

/// 重组尚未完成时返回的进度快照。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PendingStatus {
    pub key: Key,
    /// 已缓存的不同分片数（重复片不计）。
    pub fragment_count: usize,
    /// 尾片已到达则为已知总长度，否则为 `None`。
    pub total_length: Option<usize>,
    /// 已缓存的分片总字节数（按片原始长度计费）。
    pub buffered_bytes: usize,
    /// 从偏移 0 起连续覆盖的字节数。
    pub contiguous_from_zero: usize,
}

/// 送入一片后的结果：继续等待，或已重组完成。
#[derive(Debug, Clone)]
pub enum AddResult {
    Pending(PendingStatus),
    Completed(ReassembledDatagram),
}

impl AddResult {
    pub fn is_pending(&self) -> bool {
        matches!(self, AddResult::Pending(_))
    }

    pub fn is_completed(&self) -> bool {
        matches!(self, AddResult::Completed(_))
    }

    pub fn as_completed(&self) -> Option<&ReassembledDatagram> {
        match self {
            AddResult::Completed(d) => Some(d),
            _ => None,
        }
    }

    pub fn into_completed(self) -> Option<ReassembledDatagram> {
        match self {
            AddResult::Completed(d) => Some(d),
            _ => None,
        }
    }
}

/// 重组错误（显式分类，全部实现 [`std::error::Error`]）。
#[derive(Debug)]
pub enum ReassemblyError {
    /// 原始 IPv4 报文解析失败。
    ParseFailed(Ipv4Error),
    /// 分片字段本身不合法（非 8 倍数偏移、MF=1 空片等）。
    InvalidFragment(String),
    /// 非完全重复的交叠片；该数据报已被整体丢弃。
    OverlapConflict { key: Key, detail: String },
    /// 单组数据报超过配置的最大载荷。
    OversizedDatagram {
        key: Key,
        attempted_end: usize,
        max_payload: usize,
    },
    /// 全引擎内存预算不足。
    BudgetExceeded {
        budget: usize,
        used: usize,
        requested: usize,
    },
}

impl std::fmt::Display for ReassemblyError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ReassemblyError::ParseFailed(e) => write!(f, "IPv4 parse failed: {e}"),
            ReassemblyError::InvalidFragment(m) => write!(f, "invalid fragment: {m}"),
            ReassemblyError::OverlapConflict { key, detail } => {
                write!(f, "overlapping fragments for {key}: {detail}")
            }
            ReassemblyError::OversizedDatagram {
                key,
                attempted_end,
                max_payload,
            } => write!(
                f,
                "datagram for {key} exceeds max payload: end offset {attempted_end} > {max_payload}"
            ),
            ReassemblyError::BudgetExceeded {
                budget,
                used,
                requested,
            } => write!(
                f,
                "memory budget exceeded: budget={budget} used={used} requested={requested}"
            ),
        }
    }
}

impl std::error::Error for ReassemblyError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            ReassemblyError::ParseFailed(e) => Some(e),
            _ => None,
        }
    }
}

/// 单个缓存分片。
#[derive(Debug, Clone)]
struct StoredFragment {
    offset: usize,
    data: Vec<u8>,
    /// 该片是否置 MF；重组完成判定走 `Pending.total`，该字段保留用于
    /// 诊断与未来策略（如 RFC 5722）。
    #[allow(dead_code)]
    mf: bool,
}

impl StoredFragment {
    fn end(&self) -> usize {
        self.offset + self.data.len()
    }
}

/// 一个未完成数据报的重组上下文。
#[derive(Debug)]
struct Pending {
    fragments: Vec<StoredFragment>, // 始终按 offset 升序、互不交叠
    total: Option<usize>,
    created_ms: u64,
    updated_ms: u64,
    /// 本组已计费字节（等于各分片 data.len() 之和）。
    memory_bytes: usize,
}

impl Pending {
    fn new(now_ms: u64) -> Self {
        Pending {
            fragments: Vec::new(),
            total: None,
            created_ms: now_ms,
            updated_ms: now_ms,
            memory_bytes: 0,
        }
    }

    /// 从偏移 0 起连续覆盖的长度。
    fn contiguous_from_zero(&self) -> usize {
        let mut cursor = 0usize;
        for f in &self.fragments {
            // 片间互不交叠，故严格要求 f.offset == cursor
            if f.offset != cursor {
                break;
            }
            cursor = f.end();
        }
        cursor
    }
}

/// 分片重组引擎。
#[derive(Debug)]
pub struct Reassembler {
    config: Config,
    policy: OverlapPolicy,
    datagrams: HashMap<Key, Pending>,
    total_buffered: usize,
}

impl Reassembler {
    /// 以给定配置新建引擎。
    pub fn new(config: Config) -> Self {
        Reassembler {
            config,
            policy: OverlapPolicy::Reject,
            datagrams: HashMap::new(),
            total_buffered: 0,
        }
    }

    /// 使用默认配置（见 [`Config::default`]）。
    pub fn with_defaults() -> Self {
        Self::new(Config::default())
    }

    /// 当前活跃（未完成、未过期）数据报组数。
    pub fn pending_count(&self) -> usize {
        self.datagrams.len()
    }

    /// 全引擎已缓存分片字节数。
    pub fn buffered_bytes(&self) -> usize {
        self.total_buffered
    }

    /// 配置引用。
    pub fn config(&self) -> &Config {
        &self.config
    }

    /// 重叠策略（当前恒为 [`OverlapPolicy::Reject`]）。
    pub fn policy(&self) -> OverlapPolicy {
        self.policy
    }

    /// 送入一个**原始 IPv4 报文**（内部先用 [`ipv4::parse_ipv4`]
    /// 手写解析）。未分片完整报文（MF=0 且偏移 0）立即作为
    /// [`AddResult::Completed`] 返回。
    pub fn add_packet(&mut self, packet: &[u8], now_ms: u64) -> Result<AddResult, ReassemblyError> {
        self.purge_expired(now_ms);

        let parsed = ipv4::parse_ipv4(packet).map_err(ReassemblyError::ParseFailed)?;
        if !parsed.header.is_fragment() {
            return Ok(AddResult::Completed(ReassembledDatagram {
                key: Self::key_of(&parsed.header),
                payload: parsed.payload.to_vec(),
                first_arrival_ms: now_ms,
                completed_at_ms: now_ms,
                fragment_count: 1,
            }));
        }
        self.add_fragment(&parsed.header, parsed.payload, now_ms)
    }

    /// 直接送入一个已解析的分片（偏移以**字节**计）。
    pub fn add_fragment(
        &mut self,
        header: &Ipv4Header,
        payload: &[u8],
        now_ms: u64,
    ) -> Result<AddResult, ReassemblyError> {
        self.purge_expired(now_ms);

        let key = Self::key_of(header);
        let offset = header.fragment_offset;
        let mf = header.mf;
        let end = offset + payload.len();

        // ---- 分片字段合法性 ----
        if !offset.is_multiple_of(8) {
            return Err(ReassemblyError::InvalidFragment(format!(
                "fragment offset {offset} is not a multiple of 8"
            )));
        }
        if mf && payload.is_empty() {
            return Err(ReassemblyError::InvalidFragment(
                "non-final fragment (MF=1) must carry data".into(),
            ));
        }
        if end > self.config.max_datagram_payload {
            return Err(ReassemblyError::OversizedDatagram {
                key,
                attempted_end: end,
                max_payload: self.config.max_datagram_payload,
            });
        }

        // ---- ID 重用 / 尾片总长度冲突：判定后整体重启该组 ----
        if self.looks_like_id_reuse(key, offset, mf, end, payload) {
            self.drop_datagram(&key);
        }

        let is_exact_duplicate = self
            .datagrams
            .get(&key)
            .map(|p| {
                p.fragments
                    .iter()
                    .any(|f| f.offset == offset && f.end() == end && f.data.as_slice() == payload)
            })
            .unwrap_or(false);

        if is_exact_duplicate {
            // 幂等：不重复计费，仅刷新寿命。
            let p = self
                .datagrams
                .get_mut(&key)
                .expect("present after duplicate check");
            p.updated_ms = now_ms;
            return Ok(AddResult::Pending(status_of(&key, p)));
        }

        // ---- 重叠拒绝（整体丢弃该数据报）----
        if let Some(detail) = self.overlap_detail(&key, offset, end, payload) {
            self.drop_datagram(&key);
            return Err(ReassemblyError::OverlapConflict { key, detail });
        }

        // ---- 内存预算：先全引擎，再单组 ----
        if self.total_buffered + payload.len() > self.config.total_memory_budget {
            return Err(ReassemblyError::BudgetExceeded {
                budget: self.config.total_memory_budget,
                used: self.total_buffered,
                requested: payload.len(),
            });
        }
        let group_used = self
            .datagrams
            .get(&key)
            .map(|p| p.memory_bytes)
            .unwrap_or(0);
        if group_used + payload.len() > self.config.max_datagram_payload {
            return Err(ReassemblyError::OversizedDatagram {
                key,
                attempted_end: group_used + payload.len(),
                max_payload: self.config.max_datagram_payload,
            });
        }

        self.insert_fragment(key, offset, mf, payload.to_vec(), end, now_ms)?;
        self.finish_or_pending(&key, now_ms)
    }

    /// 移除所有 `updated_ms + ttl <= now_ms` 的组，返回被清理的键和
    /// 释放的字节数。
    pub fn purge_expired(&mut self, now_ms: u64) -> Vec<(Key, usize)> {
        let ttl = self.config.fragment_ttl_ms;
        let mut purged = Vec::new();
        let expired: Vec<Key> = self
            .datagrams
            .iter()
            .filter(|(_, p)| now_ms.saturating_sub(p.updated_ms) >= ttl)
            .map(|(k, _)| *k)
            .collect();
        for key in expired {
            if let Some(p) = self.datagrams.remove(&key) {
                self.total_buffered -= p.memory_bytes;
                purged.push((key, p.memory_bytes));
            }
        }
        purged
    }

    /// 查询某组的进度（不存在返回 `None`）。
    pub fn status(&self, key: &Key) -> Option<PendingStatus> {
        self.datagrams.get(key).map(|p| status_of(key, p))
    }

    /// 清空全部重组状态（释放内存预算）。
    pub fn reset(&mut self) {
        self.datagrams.clear();
        self.total_buffered = 0;
    }

    // ---------- 内部实现 ----------

    fn key_of(h: &Ipv4Header) -> Key {
        Key {
            src: h.src,
            dst: h.dst,
            protocol: h.protocol,
            identification: h.identification,
        }
    }

    /// ID 重用判定（见模块文档）：
    /// 1. 本组已缓存过不同内容的首片，又来了一个首片；
    /// 2. 新片越过本组已知总长度。
    fn looks_like_id_reuse(
        &self,
        key: Key,
        offset: usize,
        mf: bool,
        end: usize,
        payload: &[u8],
    ) -> bool {
        let Some(p) = self.datagrams.get(&key) else {
            return false;
        };

        // 情形 1：新首片 vs 已缓存首片
        if offset == 0 && mf {
            if let Some(existing_head) = p.fragments.iter().find(|f| f.offset == 0) {
                let n = existing_head.data.len().min(payload.len());
                if existing_head.data[..n] != payload[..n] || existing_head.end() != end {
                    return true;
                }
            }
        }

        // 情形 2：越过已知总长度
        if let Some(total) = p.total {
            if end > total {
                return true;
            }
        }
        false
    }

    /// 若本片与任一已缓存片交叠（完全重复已在外层排除），返回原因。
    fn overlap_detail(
        &self,
        key: &Key,
        offset: usize,
        end: usize,
        payload: &[u8],
    ) -> Option<String> {
        let p = self.datagrams.get(key)?;
        for f in &p.fragments {
            let lo = offset.max(f.offset);
            let hi = end.min(f.end());
            if lo < hi {
                let new_seg = &payload[lo - offset..hi - offset];
                let old_seg = &f.data[lo - f.offset..hi - f.offset];
                let same = new_seg == old_seg;
                return Some(format!(
                    "incoming [{},{}) overlaps stored [{},{}) with {} bytes (content {})",
                    offset,
                    end,
                    f.offset,
                    f.end(),
                    hi - lo,
                    if same { "identical" } else { "CONFLICTING" }
                ));
            }
        }
        None
    }

    fn insert_fragment(
        &mut self,
        key: Key,
        offset: usize,
        mf: bool,
        data: Vec<u8>,
        end: usize,
        now_ms: u64,
    ) -> Result<(), ReassemblyError> {
        let p = self
            .datagrams
            .entry(key)
            .or_insert_with(|| Pending::new(now_ms));
        p.memory_bytes += data.len();
        self.total_buffered += data.len();
        if !mf {
            p.total = Some(end);
        }
        p.updated_ms = now_ms;

        // 保持按 offset 升序（此时与旧片必然不相交、不重复）
        let pos = p.fragments.partition_point(|f| f.offset < offset);
        p.fragments.insert(pos, StoredFragment { offset, data, mf });
        Ok(())
    }

    fn finish_or_pending(&mut self, key: &Key, now_ms: u64) -> Result<AddResult, ReassemblyError> {
        let p = self.datagrams.get(key).expect("pending group must exist");

        // 尾片未到：总长度未知，不可能完成。
        let Some(total) = p.total else {
            return Ok(AddResult::Pending(status_of(key, p)));
        };

        // 片间不相交且升序：必须严丝合缝覆盖 [0, total)。
        let mut cursor = 0usize;
        for f in &p.fragments {
            if f.offset != cursor {
                return Ok(AddResult::Pending(status_of(key, p)));
            }
            cursor = f.end();
        }
        if cursor != total {
            return Ok(AddResult::Pending(status_of(key, p)));
        }

        // 重组：按序拷贝。
        let p = self.datagrams.remove(key).expect("group present");
        self.total_buffered -= p.memory_bytes;
        let fragment_count = p.fragments.len();
        let first_arrival_ms = p.created_ms;
        let mut payload = Vec::with_capacity(total);
        for f in &p.fragments {
            payload.extend_from_slice(&f.data);
        }
        debug_assert_eq!(payload.len(), total);

        Ok(AddResult::Completed(ReassembledDatagram {
            key: *key,
            payload,
            first_arrival_ms,
            completed_at_ms: now_ms,
            fragment_count,
        }))
    }

    fn drop_datagram(&mut self, key: &Key) {
        if let Some(p) = self.datagrams.remove(key) {
            self.total_buffered -= p.memory_bytes;
        }
    }
}

/// 由缓存状态构造进度快照（自由函数，便于在可变借用后直接调用）。
fn status_of(key: &Key, p: &Pending) -> PendingStatus {
    PendingStatus {
        key: *key,
        fragment_count: p.fragments.len(),
        total_length: p.total,
        buffered_bytes: p.memory_bytes,
        contiguous_from_zero: p.contiguous_from_zero(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SRC: (u8, u8, u8, u8) = (192, 168, 0, 1);
    const DST: (u8, u8, u8, u8) = (10, 0, 0, 200);

    fn fragment(id: u16, off: usize, mf: bool, data: &[u8]) -> Vec<u8> {
        ipv4::build_fragment(SRC, DST, 17, id, off, mf, data.to_vec())
    }

    fn key_of_id(id: u16) -> Key {
        Key {
            src: Ipv4Addr::from([SRC.0, SRC.1, SRC.2, SRC.3]),
            dst: Ipv4Addr::from([DST.0, DST.1, DST.2, DST.3]),
            protocol: 17,
            identification: id,
        }
    }

    /// 标准引擎：短 TTL 便于测试过期。
    fn engine() -> Reassembler {
        Reassembler::new(Config {
            fragment_ttl_ms: 1_000,
            total_memory_budget: 1 << 20,
            max_datagram_payload: 65_535,
        })
    }

    fn expect_pending(r: AddResult) -> PendingStatus {
        match r {
            AddResult::Pending(s) => s,
            AddResult::Completed(d) => panic!("expected pending, got {} bytes", d.payload.len()),
        }
    }

    fn expect_completed(r: AddResult) -> ReassembledDatagram {
        r.into_completed().expect("expected completed")
    }

    #[test]
    fn reassembles_in_order() {
        let mut e = engine();
        let payload: Vec<u8> = (0u8..=255).cycle().take(3000).collect();
        let p1 = fragment(1, 0, true, &payload[..1480]);
        let p2 = fragment(1, 1480, true, &payload[1480..2960]);
        let p3 = fragment(1, 2960, false, &payload[2960..]);

        assert!(e.add_packet(&p1, 0).unwrap().is_pending());
        assert!(e.add_packet(&p2, 1).unwrap().is_pending());
        let d = expect_completed(e.add_packet(&p3, 2).unwrap());
        assert_eq!(d.payload, payload);
        assert_eq!(d.fragment_count, 3);
        assert_eq!(e.buffered_bytes(), 0);
    }

    #[test]
    fn reassembles_out_of_order() {
        // 验收项 1：乱序
        let mut e = engine();
        let payload: Vec<u8> = (0..5000u32).map(|x| (x % 251) as u8).collect();
        let cuts = [0usize, 1480, 2960, 4440, 5000];
        let mut pkts: Vec<_> = (0..4)
            .map(|i| fragment(7, cuts[i], i < 3, &payload[cuts[i]..cuts[i + 1]]))
            .collect();
        pkts.reverse(); // 尾片先到
        pkts.rotate_left(1); // 再打乱：片3、片1、片4、片2 之类

        let mut last = None;
        for (i, pkt) in pkts.iter().enumerate() {
            let r = e.add_packet(pkt, i as u64).unwrap();
            if let AddResult::Completed(d) = r {
                last = Some(d);
            }
        }
        let d = last.expect("should complete despite reordering");
        assert_eq!(
            d.payload, payload,
            "reassembled payload must match original"
        );
    }

    #[test]
    fn duplicate_fragments_are_idempotent() {
        // 验收项 2：重复片（几何合法：非尾片 104 字节，8 的倍数）
        let mut e = engine();
        let p1 = fragment(2, 0, true, &[0xAA; 104]);
        let p2 = fragment(2, 104, false, &[0xBB; 50]);

        let s = expect_pending(e.add_packet(&p1, 0).unwrap());
        assert_eq!(s.fragment_count, 1);
        assert_eq!(s.contiguous_from_zero, 104);

        let s = expect_pending(e.add_packet(&p1, 5).unwrap()); // 完全重复
        assert_eq!(s.fragment_count, 1, "duplicate must not add a fragment");
        assert_eq!(e.buffered_bytes(), 104, "duplicate must not be charged");

        let s = expect_pending(e.add_packet(&p1, 9).unwrap()); // 再来一次
        assert_eq!(s.fragment_count, 1);

        let d = expect_completed(e.add_packet(&p2, 10).unwrap());
        assert_eq!(&d.payload[..104], &[0xAA; 104]);
        assert_eq!(&d.payload[104..], &[0xBB; 50]);
    }

    #[test]
    fn overlapping_fragments_drop_whole_datagram() {
        // 验收项 3：重叠（内容冲突）
        let mut e = engine();
        let p1 = fragment(3, 0, true, &[0x01; 104]);
        // 偏移 96 与 p1 的 [0,104) 交叠 8 字节，且内容不同
        let bad = fragment(3, 96, true, &[0x02; 104]);
        let p2 = fragment(3, 200, false, &[0x03; 8]);

        e.add_packet(&p1, 0).unwrap();
        let err = e.add_packet(&bad, 1).unwrap_err();
        assert!(
            matches!(err, ReassemblyError::OverlapConflict { .. }),
            "{err}"
        );
        assert_eq!(e.pending_count(), 0, "conflicted datagram must be dropped");
        assert_eq!(e.buffered_bytes(), 0);

        // 同组后续片不再“缝合”旧数据（旧组已丢弃，尾片单独到达也完不成）
        let s = expect_pending(e.add_packet(&p2, 2).unwrap());
        assert_eq!(s.fragment_count, 1);
        assert_eq!(s.contiguous_from_zero, 0);
    }

    #[test]
    fn identical_partial_overlap_is_still_rejected() {
        // 即使交叠区字节相同，非完全重复也拒绝（明确策略）
        let mut e = engine();
        e.add_packet(&fragment(4, 0, true, &[0x55; 104]), 0)
            .unwrap();
        let err = e
            .add_packet(&fragment(4, 96, true, &[0x55; 104]), 1)
            .unwrap_err();
        assert!(matches!(err, ReassemblyError::OverlapConflict { .. }));
    }

    #[test]
    fn missing_first_fragment_never_completes() {
        // 验收项 4：缺首片
        let mut e = engine();
        let mid = fragment(5, 1480, true, &[0xCC; 1480]);
        let tail = fragment(5, 2960, false, &[0xDD; 40]);

        let s = expect_pending(e.add_packet(&mid, 0).unwrap());
        assert_eq!(s.contiguous_from_zero, 0);
        let s = expect_pending(e.add_packet(&tail, 1).unwrap());
        assert_eq!(s.total_length, Some(3000));
        assert_eq!(s.contiguous_from_zero, 0, "gap at offset 0 remains");
        assert!(s.total_length.is_some());

        // 补上首片后立刻完成，且与原始载荷一致
        let head = fragment(5, 0, true, &[0xAA; 1480]);
        let mut original = vec![0xAA; 1480];
        original.extend(std::iter::repeat_n(0xCC, 1480));
        original.extend(std::iter::repeat_n(0xDD, 40));
        let d = expect_completed(e.add_packet(&head, 2).unwrap());
        assert_eq!(d.payload, original);
    }

    #[test]
    fn id_reuse_replaces_old_datagram() {
        // 验收项 5：ID 重用——旧组未完成，新数据报重用四元组+ID
        let mut e = engine();
        // 旧数据报：首片与尾片之间有洞
        let old_head = fragment(9, 0, true, &[0x11; 16]);
        let old_tail = fragment(9, 32, false, &[0x22; 8]);
        e.add_packet(&old_head, 0).unwrap();
        e.add_packet(&old_tail, 1).unwrap();
        assert_eq!(e.pending_count(), 1);

        // 新数据报复用 id=9：首片内容完全不同 → 判定重用
        let new_head = fragment(9, 0, true, &[0x99; 16]);
        let s = expect_pending(e.add_packet(&new_head, 2).unwrap());
        assert_eq!(s.fragment_count, 1, "old fragments must be discarded");

        let new_tail = fragment(9, 16, false, &[0x88; 16]);
        let d = expect_completed(e.add_packet(&new_tail, 3).unwrap());
        let mut expected = vec![0x99; 16];
        expected.extend(std::iter::repeat_n(0x88, 16));
        assert_eq!(d.payload, expected);
    }

    #[test]
    fn id_reuse_due_to_length_change() {
        // 另一类重用：旧组尾片已声明总长 16，新数据报的片越过 16
        let mut e = engine();
        // 旧数据报：只有尾片 [8,16)，总长已知为 16
        e.add_packet(&fragment(11, 8, false, &[0x02; 8]), 0)
            .unwrap();
        assert_eq!(e.status(&key_of_id(11)).unwrap().total_length, Some(16));
        // 新数据报：首片 MF=1，携带 24 字节，end=24 > 16 → ID 重用重启
        e.add_packet(&fragment(11, 0, true, &[0x01; 24]), 1)
            .unwrap();
        let s = e.status(&key_of_id(11)).unwrap();
        assert_eq!(s.fragment_count, 1, "old tail must be discarded");
        assert_eq!(s.buffered_bytes, 24);
        assert_eq!(s.total_length, None, "new group has no tail yet");
    }

    #[test]
    fn expired_groups_are_purged_lazily_and_explicitly() {
        // 验收项 6：超时清理
        let mut e = engine(); // TTL=1000ms
        e.add_packet(&fragment(20, 0, true, &[0xAA; 8]), 0).unwrap();
        assert_eq!(e.pending_count(), 1);

        // 999ms：还活着，且到达重复片会刷新寿命
        let purged = e.purge_expired(999);
        assert!(purged.is_empty());

        let purged = e.purge_expired(1_000);
        assert_eq!(purged.len(), 1);
        assert_eq!(purged[0].1, 8, "freed bytes returned");
        assert_eq!(e.pending_count(), 0);
        assert_eq!(e.buffered_bytes(), 0);

        // 惰性：add 时也会清理过期组
        e.add_packet(&fragment(21, 0, true, &[0xBB; 8]), 10_000)
            .unwrap();
        e.add_packet(&fragment(21, 8, false, &[0xCC; 8]), 20_000 - 1)
            .unwrap();
        assert_eq!(e.pending_count(), 1);
        // 下一次 add 触发清理（此时 21 组最后更新于 19999，+1000 = 20999）
        e.add_packet(&fragment(22, 0, true, &[0xDD; 8]), 21_000)
            .unwrap();
        assert_eq!(e.pending_count(), 1, "group 21 expired lazily, 22 remains");
    }

    #[test]
    fn ttl_refresh_keeps_busy_group_alive() {
        let mut e = engine();
        e.add_packet(&fragment(30, 0, true, &[0; 8]), 0).unwrap();
        e.add_packet(&fragment(30, 8, true, &[0; 8]), 999).unwrap(); // 刷新寿命至 1999
                                                                     // 距最后一片 501ms：仍然存活
        assert!(e.purge_expired(1_500).is_empty());
        // 1998ms：仍在寿命内
        assert!(e.purge_expired(1_998).is_empty());
        // 1999ms：到期（now - updated == ttl）
        assert_eq!(e.purge_expired(1_999).len(), 1);
    }

    #[test]
    fn total_memory_budget_is_enforced() {
        let mut e = Reassembler::new(Config {
            fragment_ttl_ms: 10_000,
            total_memory_budget: 100,
            max_datagram_payload: 65_535,
        });
        // 不同四元组各吃 80 字节
        e.add_packet(&fragment(40, 0, true, &[0; 80]), 0).unwrap();
        let err = e
            .add_packet(&fragment(41, 0, true, &[0; 80]), 1)
            .unwrap_err();
        assert!(
            matches!(err, ReassemblyError::BudgetExceeded { .. }),
            "{err}"
        );
        assert_eq!(e.buffered_bytes(), 80, "rejected fragment not charged");
    }

    #[test]
    fn oversized_datagram_is_rejected() {
        let mut e = Reassembler::new(Config {
            fragment_ttl_ms: 10_000,
            total_memory_budget: 1 << 20,
            max_datagram_payload: 16,
        });
        let err = e
            .add_packet(&fragment(42, 0, true, &[0; 24]), 0)
            .unwrap_err();
        assert!(matches!(err, ReassemblyError::OversizedDatagram { .. }));
    }

    #[test]
    fn unfragmented_packet_completes_immediately() {
        let pkt = ipv4::build_fragment(SRC, DST, 1, 77, 0, false, vec![9; 33]);
        let mut e = engine();
        let d = expect_completed(e.add_packet(&pkt, 0).unwrap());
        assert_eq!(d.payload, vec![9; 33]);
        assert_eq!(d.fragment_count, 1);
        assert_eq!(e.pending_count(), 0);
    }

    #[test]
    fn malformed_packet_is_parse_error() {
        let mut e = engine();
        let err = e.add_packet(&[0x45u8, 0x00, 0x00], 0).unwrap_err();
        assert!(matches!(err, ReassemblyError::ParseFailed(_)));
    }

    #[test]
    fn completed_payload_matches_full_original_for_various_sizes() {
        // 对照完整原始载荷：多种长度（含非 8 整除结尾）
        let mut e = engine();
        for (id, len) in [(100u16, 1usize), (101, 7), (102, 1500), (103, 4001)] {
            let original: Vec<u8> = (0..len).map(|i| (i * 31 + id as usize) as u8).collect();
            let mut off = 0usize;
            let mut pieces = Vec::new();
            while off < original.len() {
                let take = 1200.min(original.len() - off);
                let mf = off + take < original.len();
                pieces.push((off, mf, original[off..off + take].to_vec()));
                off += take;
            }
            // 乱序送入
            if pieces.len() > 1 {
                pieces.rotate_right(1);
            }
            let mut done = None;
            for (i, (o, mf, data)) in pieces.iter().enumerate() {
                let pkt = fragment(id, *o, *mf, data);
                if let AddResult::Completed(d) = e.add_packet(&pkt, i as u64).unwrap() {
                    done = Some(d);
                }
            }
            assert_eq!(
                done.expect("completed").payload,
                original,
                "id={id} len={len}"
            );
        }
    }
}
