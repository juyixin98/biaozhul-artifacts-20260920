//! # 离线 IPv4 分片重组
//!
//! 不使用原始套接字，不接触内核协议栈：重组器只消费**已经解析好的**
//! [`crate::ipv4::Ipv4Packet`]（或等价的 [`FragmentInfo`]），因此可以在
//! 任意离线/测试环境运行，也不需要任何系统权限。
//!
//! ## 分组（RFC 791）
//!
//! 数据报按四元组 **(源地址, 目的地址, 协议号, Identification)** 分组，
//! 见 [`FlowKey`]。地址族仅支持 IPv4。
//!
//! ## 重叠片：明确的拒绝策略
//!
//! 现实协议栈对重叠有 BSD“旧片优先”/ RFC 5722“新片优先”等不同行为。
//! 本实现**不做任何静默覆盖**，策略由 [`OverlapPolicy`] 显式配置：
//!
//! * [`OverlapPolicy::RejectNewFragment`]（默认）—— 新片与已有数据相交
//!   且不是“范围与内容完全相同的重复片”时，**拒绝新片**，保留原组装；
//! * [`OverlapPolicy::DropAssembly`] —— 一旦重叠，**丢弃整个组装**
//!   （它的所有缓冲字节立即归还内存预算），等待对端重传。
//!
//! ## 寿命（TTL）
//!
//! 每个组装记录 `last_update`；当 `now - last_update >= assembly_ttl_ms`
//! 时，[`ReassemblyEngine::purge_expired`] 将其回收。服务器在插入时按
//! `purge_interval` 机会式清理；测试可传入确定性时钟精确控制。
//!
//! ## 内存预算
//!
//! * `mem_budget_bytes`：所有未完成组装的负载字节总和硬上限；
//! * `max_datagram_bytes`：单个重组数据报的最大字节数；
//! * `max_assemblies`：同时存在的组装数量上限。
//!
//! 超限一律拒绝新片（或拒绝创建新组装），不会发生无界缓冲。

use crate::ipv4::Ipv4Packet;
use std::collections::HashMap;
use std::net::Ipv4Addr;

/// 重组分组键：(源, 目的, 协议号, Identification)。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct FlowKey {
    pub src: Ipv4Addr,
    pub dst: Ipv4Addr,
    pub protocol: u8,
    pub identification: u16,
}

impl std::fmt::Display for FlowKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "{} -> {} proto={} id={}",
            self.src, self.dst, self.protocol, self.identification
        )
    }
}

/// 重叠片处理策略（见模块文档）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum OverlapPolicy {
    /// 拒绝造成重叠的新片，保留已有组装。（默认）
    #[default]
    RejectNewFragment,
    /// 出现重叠即丢弃整个组装。
    DropAssembly,
}

/// 重组器配置。
#[derive(Debug, Clone)]
pub struct ReassemblerConfig {
    /// 组装寿命（毫秒）：超过该时间没有新片到达的组装将被清理。
    pub assembly_ttl_ms: u64,
    /// 两次机会式全量清理之间的最小间隔（毫秒）；0 表示每次插入都清理。
    pub purge_interval_ms: u64,
    /// 所有未完成组装负载字节总和上限。
    pub mem_budget_bytes: usize,
    /// 单个重组数据报的最大字节数。
    pub max_datagram_bytes: usize,
    /// 同时存在的组装数量上限。
    pub max_assemblies: usize,
    /// 重叠片策略。
    pub overlap_policy: OverlapPolicy,
}

impl Default for ReassemblerConfig {
    /// 保守的默认值：TTL 30s（接近常见 IP_REASM_TIMEOUT 量级），
    /// 内存预算 4 MiB，单数据报 64 KiB，组装数 1024。
    fn default() -> Self {
        ReassemblerConfig {
            assembly_ttl_ms: 30_000,
            purge_interval_ms: 1_000,
            mem_budget_bytes: 4 * 1024 * 1024,
            max_datagram_bytes: u16::MAX as usize,
            max_assemblies: 1024,
            overlap_policy: OverlapPolicy::RejectNewFragment,
        }
    }
}

/// 与解析无关的片描述（重组器的直接输入；`From<&Ipv4Packet>` 可构造）。
#[derive(Debug, Clone)]
pub struct FragmentInfo {
    pub key: FlowKey,
    /// 负载在重组数据报中的字节偏移。
    pub offset: usize,
    pub payload: Vec<u8>,
    /// 是否为末片（MF=0）。
    pub is_last: bool,
}

impl<'a> FragmentInfo {
    pub fn from_packet(pkt: &Ipv4Packet<'a>) -> FragmentInfo {
        FragmentInfo {
            key: FlowKey {
                src: pkt.src,
                dst: pkt.dst,
                protocol: pkt.protocol,
                identification: pkt.identification,
            },
            offset: pkt.fragment_offset,
            payload: pkt.payload.to_vec(),
            is_last: !pkt.more_fragments,
        }
    }
}

/// 重组过程中的明确错误类型。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReassembleError {
    /// 新片与已有片相交（且不是完全重复片）。
    Overlap {
        key: FlowKey,
        existing: (usize, usize),
        incoming: (usize, usize),
    },
    /// 新片与已有片范围相同但内容不同（数据矛盾）。
    ConflictingData { key: FlowKey, range: (usize, usize) },
    /// 已有末片声明的结束位置与新末片不一致。
    ConflictingLastFragment {
        key: FlowKey,
        previous_end: usize,
        new_end: usize,
    },
    /// 单片或数据报推算长度超过 `max_datagram_bytes`。
    DatagramTooLarge {
        key: FlowKey,
        attempted: usize,
        limit: usize,
    },
    /// 新片会让总缓冲超过全局内存预算。
    MemoryBudgetExceeded {
        used: usize,
        budget: usize,
        needed: usize,
    },
    /// 同时存在的组装数达到上限。
    TooManyAssemblies { limit: usize },
}

impl std::fmt::Display for ReassembleError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ReassembleError::Overlap {
                existing,
                incoming,
                key,
            } => write!(
                f,
                "overlapping fragment rejected for [{key}]: existing [{},{}) vs incoming [{},{})",
                existing.0, existing.1, incoming.0, incoming.1
            ),
            ReassembleError::ConflictingData { key, range } => write!(
                f,
                "fragment with same range [{},{}) but different data rejected for [{key}]",
                range.0, range.1
            ),
            ReassembleError::ConflictingLastFragment {
                key,
                previous_end,
                new_end,
            } => write!(
                f,
                "conflicting last fragments for [{key}]: previous end {previous_end}, new end {new_end}"
            ),
            ReassembleError::DatagramTooLarge {
                key,
                attempted,
                limit,
            } => write!(
                f,
                "datagram too large for [{key}]: attempted {attempted}, limit {limit}"
            ),
            ReassembleError::MemoryBudgetExceeded {
                used,
                budget,
                needed,
            } => write!(
                f,
                "memory budget exceeded: used {used}, budget {budget}, need {needed} more"
            ),
            ReassembleError::TooManyAssemblies { limit } => {
                write!(f, "too many concurrent assemblies (limit {limit})")
            }
        }
    }
}

impl std::error::Error for ReassembleError {}

/// 首片再次到达已有首片的组装时的判定。
enum FirstFragAction {
    /// 与已有首片范围、内容完全一致：重传。
    Duplicate,
    /// 不同：Identification 重用，需丢弃指定字节数的旧组装。
    Reuse(usize),
}

#[derive(Debug, Clone)]
struct Interval {
    offset: usize,
    data: Vec<u8>,
}

impl Interval {
    fn end(&self) -> usize {
        self.offset + self.data.len()
    }
    fn range(&self) -> (usize, usize) {
        (self.offset, self.end())
    }
}

/// 单个数据报的重组状态。
#[derive(Debug, Clone)]
pub struct Assembly {
    pub key: FlowKey,
    /// 已按偏移排序、互不相交的片区间。
    intervals: Vec<Interval>,
    /// 末片声明的数据报结束位置；`None` 表示末片尚未到达。
    last_end: Option<usize>,
    pub created_at_ms: u64,
    pub last_update_ms: u64,
    /// 当前为该组装缓冲的负载字节数。
    pub buffered_bytes: usize,
}

impl Assembly {
    fn new(key: FlowKey, now_ms: u64) -> Self {
        Assembly {
            key,
            intervals: Vec::new(),
            last_end: None,
            created_at_ms: now_ms,
            last_update_ms: now_ms,
            buffered_bytes: 0,
        }
    }

    /// 首片（偏移 0 的数据）是否已到达。
    pub fn has_first_fragment(&self) -> bool {
        self.intervals
            .first()
            .map(|i| i.offset == 0)
            .unwrap_or(false)
    }

    /// 末片是否已到达。
    pub fn has_last_fragment(&self) -> bool {
        self.last_end.is_some()
    }

    /// 已到达的片数。
    pub fn fragment_count(&self) -> usize {
        self.intervals.len()
    }

    /// 从偏移 0 开始的连续覆盖长度（缺首片时为 0）。
    pub fn contiguous_prefix(&self) -> usize {
        let mut cur = 0;
        for iv in &self.intervals {
            if iv.offset <= cur {
                cur = cur.max(iv.end());
            } else {
                break;
            }
        }
        cur
    }

    fn is_complete(&self) -> bool {
        match self.last_end {
            Some(end) => self.has_first_fragment() && self.contiguous_prefix() >= end,
            None => false,
        }
    }

    /// 完成时拼接完整数据报。仅在 [`Self::is_complete`] 为真时有意义。
    fn assemble(self) -> Vec<u8> {
        let total = self.last_end.expect("complete datagram has last_end");
        let mut out = Vec::with_capacity(total);
        for iv in self.intervals {
            // intervals 互不相交且按序连续覆盖 [0,total)
            debug_assert_eq!(iv.offset, out.len());
            out.extend_from_slice(&iv.data);
        }
        debug_assert_eq!(out.len(), total);
        out
    }
}

/// 查询单个组装时返回的快照。
#[derive(Debug, Clone)]
pub struct AssemblyStatus {
    pub key: FlowKey,
    pub buffered_bytes: usize,
    pub fragment_count: usize,
    pub has_first_fragment: bool,
    pub has_last_fragment: bool,
    pub contiguous_prefix: usize,
    pub expected_total: Option<usize>,
    pub age_ms: u64,
}

/// 一次成功插入产生的事件。
#[derive(Debug, Clone)]
pub enum ReassembleEvent {
    /// 片被接受并进入组装（尚未完成）。
    Accepted {
        key: FlowKey,
        buffered_bytes: usize,
        fragment_count: usize,
        has_first: bool,
        has_last: bool,
    },
    /// 完全重复的片（范围与内容一致），被忽略。
    Duplicate { key: FlowKey },
    /// 首片在已有组装中再次到达：判定为 Identification 重用，旧组装被重置。
    IdReuseReset {
        key: FlowKey,
        discarded_bytes: usize,
    },
    /// 数据报重组完成；组装随即从表中移除。
    Completed {
        key: FlowKey,
        data: Vec<u8>,
        fragment_count: usize,
    },
}

/// 过期清理结果。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PurgeReport {
    pub removed_assemblies: usize,
    pub reclaimed_bytes: usize,
    pub keys: Vec<FlowKey>,
}

/// 运行期统计计数器。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Stats {
    pub fragments_received: u64,
    pub duplicates: u64,
    pub overlaps_rejected: u64,
    pub datagrams_completed: u64,
    pub id_reuse_resets: u64,
    pub assemblies_expired: u64,
    pub bytes_reassembled: u64,
}

/// 分片重组引擎。所有时间戳由调用方注入（毫秒），便于确定性测试。
#[derive(Debug)]
pub struct ReassemblyEngine {
    config: ReassemblerConfig,
    assemblies: HashMap<FlowKey, Assembly>,
    /// 全部未完成组装当前缓冲的负载字节总数。
    bytes_used: usize,
    last_purge_ms: u64,
    /// 已见过的最大时间戳，防止时钟倒退。
    high_water_ms: u64,
    pub stats: Stats,
}

impl ReassemblyEngine {
    pub fn new(config: ReassemblerConfig) -> Self {
        ReassemblyEngine {
            config,
            assemblies: HashMap::new(),
            bytes_used: 0,
            last_purge_ms: 0,
            high_water_ms: 0,
            stats: Stats::default(),
        }
    }

    pub fn config(&self) -> &ReassemblerConfig {
        &self.config
    }

    /// 当前未完成组装使用的负载字节数。
    pub fn bytes_used(&self) -> usize {
        self.bytes_used
    }

    /// 当前未完成组装数量。
    pub fn assembly_count(&self) -> usize {
        self.assemblies.len()
    }

    fn tick(&mut self, now_ms: u64) -> Option<PurgeReport> {
        if now_ms < self.high_water_ms {
            // 时钟倒退：忽略，不清理。
            return None;
        }
        self.high_water_ms = now_ms;
        if now_ms.saturating_sub(self.last_purge_ms) >= self.config.purge_interval_ms {
            self.last_purge_ms = now_ms;
            Some(self.purge_expired(now_ms))
        } else {
            None
        }
    }

    /// 删除寿命到期的组装，返回清理报告。
    pub fn purge_expired(&mut self, now_ms: u64) -> PurgeReport {
        let mut report = PurgeReport::default();
        let ttl = self.config.assembly_ttl_ms;
        let expired_keys: Vec<FlowKey> = self
            .assemblies
            .iter()
            .filter(|(_, a)| now_ms.saturating_sub(a.last_update_ms) >= ttl)
            .map(|(k, _)| *k)
            .collect();
        for k in expired_keys {
            if let Some(a) = self.assemblies.remove(&k) {
                report.removed_assemblies += 1;
                report.reclaimed_bytes += a.buffered_bytes;
                report.keys.push(a.key);
            }
        }
        self.bytes_used = self.bytes_used.saturating_sub(report.reclaimed_bytes);
        self.stats.assemblies_expired += report.removed_assemblies as u64;
        report
    }

    /// 直接喂入解析好的 IPv4 报文。
    pub fn insert_packet<'a>(
        &mut self,
        pkt: &Ipv4Packet<'a>,
        now_ms: u64,
    ) -> Result<ReassembleEvent, ReassembleError> {
        self.insert_fragment(FragmentInfo::from_packet(pkt), now_ms)
    }

    /// 核心入口：插入一个片。
    pub fn insert_fragment(
        &mut self,
        frag: FragmentInfo,
        now_ms: u64,
    ) -> Result<ReassembleEvent, ReassembleError> {
        let _ = self.tick(now_ms);
        self.stats.fragments_received += 1;

        let key = frag.key;
        let start = frag.offset;
        let end = start + frag.payload.len();

        // ---- 单片/数据报大小上限 ----
        if end > self.config.max_datagram_bytes {
            return Err(ReassembleError::DatagramTooLarge {
                key,
                attempted: end,
                limit: self.config.max_datagram_bytes,
            });
        }

        // ---- Identification 重用 vs 首片重传 ----
        // 首片再次到达一个“已有首片”的未完成组装时：
        //   * 范围与内容完全一致 -> 重传（重复片），忽略并续命；
        //   * 否则              -> 判定为 Identification 重用，重置旧组装。
        let mut reset_bytes = 0;
        if start == 0 {
            let decision = self.assemblies.get(&key).and_then(|existing| {
                if !existing.has_first_fragment() {
                    return None;
                }
                let identical = existing
                    .intervals
                    .iter()
                    .any(|iv| iv.offset == 0 && iv.end() == end && iv.data == frag.payload);
                Some(if identical {
                    FirstFragAction::Duplicate
                } else {
                    FirstFragAction::Reuse(existing.buffered_bytes)
                })
            });
            match decision {
                Some(FirstFragAction::Duplicate) => {
                    if let Some(a) = self.assemblies.get_mut(&key) {
                        a.last_update_ms = now_ms;
                    }
                    self.stats.duplicates += 1;
                    return Ok(ReassembleEvent::Duplicate { key });
                }
                Some(FirstFragAction::Reuse(bytes)) => {
                    reset_bytes = bytes;
                    self.assemblies.remove(&key);
                    self.bytes_used = self.bytes_used.saturating_sub(reset_bytes);
                    self.stats.id_reuse_resets += 1;
                }
                None => {}
            }
        }

        // ---- 取得或创建组装 ----
        let assembly = if let Some(a) = self.assemblies.get_mut(&key) {
            a
        } else {
            if self.assemblies.len() >= self.config.max_assemblies {
                return Err(ReassembleError::TooManyAssemblies {
                    limit: self.config.max_assemblies,
                });
            }
            self.assemblies
                .entry(key)
                .or_insert(Assembly::new(key, now_ms))
        };

        // ---- 末片一致性 ----
        if frag.is_last {
            if let Some(prev_end) = assembly.last_end {
                if prev_end != end {
                    return Err(ReassembleError::ConflictingLastFragment {
                        key,
                        previous_end: prev_end,
                        new_end: end,
                    });
                }
            }
        }

        // ---- 完全重复片 / 矛盾片 / 重叠片判定 ----
        if let Some(hit) = assembly
            .intervals
            .iter()
            .find(|iv| iv.range() == (start, end))
        {
            if hit.data == frag.payload {
                assembly.last_update_ms = now_ms;
                self.stats.duplicates += 1;
                return Ok(ReassembleEvent::Duplicate { key });
            } else {
                self.stats.overlaps_rejected += 1;
                return Err(ReassembleError::ConflictingData {
                    key,
                    range: (start, end),
                });
            }
        }

        if let Some(hit) = assembly
            .intervals
            .iter()
            .find(|iv| start < iv.end() && iv.offset < end)
        {
            let existing = hit.range();
            self.stats.overlaps_rejected += 1;
            return match self.config.overlap_policy {
                OverlapPolicy::RejectNewFragment => Err(ReassembleError::Overlap {
                    key,
                    existing,
                    incoming: (start, end),
                }),
                OverlapPolicy::DropAssembly => {
                    let bytes = assembly.buffered_bytes;
                    self.assemblies.remove(&key);
                    self.bytes_used = self.bytes_used.saturating_sub(bytes);
                    Err(ReassembleError::Overlap {
                        key,
                        existing,
                        incoming: (start, end),
                    })
                }
            };
        }

        // ---- 内存预算（含末片带来的数据报总长度推算）----
        let projected_end = assembly
            .last_end
            .unwrap_or(0)
            .max(end)
            .max(assembly.intervals.last().map(|iv| iv.end()).unwrap_or(0));
        if projected_end > self.config.max_datagram_bytes {
            return Err(ReassembleError::DatagramTooLarge {
                key,
                attempted: projected_end,
                limit: self.config.max_datagram_bytes,
            });
        }
        if self.bytes_used.saturating_add(frag.payload.len()) > self.config.mem_budget_bytes {
            return Err(ReassembleError::MemoryBudgetExceeded {
                used: self.bytes_used,
                budget: self.config.mem_budget_bytes,
                needed: frag.payload.len(),
            });
        }

        // ---- 接受片 ----
        let pos = assembly.intervals.partition_point(|iv| iv.offset < start);
        assembly.intervals.insert(
            pos,
            Interval {
                offset: start,
                data: frag.payload,
            },
        );
        assembly.buffered_bytes += end - start;
        assembly.last_update_ms = now_ms;
        if frag.is_last {
            assembly.last_end = Some(end);
        }
        self.bytes_used += end - start;

        // ---- 完成判定 ----
        let fragment_count = assembly.fragment_count();
        let buffered = assembly.buffered_bytes;
        let has_first = assembly.has_first_fragment();
        let has_last = assembly.has_last_fragment();

        if assembly.is_complete() {
            let finished = self.assemblies.remove(&key).expect("assembly present");
            let total = finished.buffered_bytes;
            self.bytes_used = self.bytes_used.saturating_sub(total);
            let data = finished.assemble();
            self.stats.datagrams_completed += 1;
            self.stats.bytes_reassembled += data.len() as u64;
            return Ok(ReassembleEvent::Completed {
                key,
                data,
                fragment_count,
            });
        }

        if reset_bytes > 0 {
            Ok(ReassembleEvent::IdReuseReset {
                key,
                discarded_bytes: reset_bytes,
            })
        } else {
            Ok(ReassembleEvent::Accepted {
                key,
                buffered_bytes: buffered,
                fragment_count,
                has_first,
                has_last,
            })
        }
    }

    /// 查询某个未完成组装的状态。
    pub fn status(&self, key: &FlowKey, now_ms: u64) -> Option<AssemblyStatus> {
        self.assemblies.get(key).map(|a| AssemblyStatus {
            key: a.key,
            buffered_bytes: a.buffered_bytes,
            fragment_count: a.fragment_count(),
            has_first_fragment: a.has_first_fragment(),
            has_last_fragment: a.has_last_fragment(),
            contiguous_prefix: a.contiguous_prefix(),
            expected_total: a.last_end,
            age_ms: now_ms.saturating_sub(a.created_at_ms),
        })
    }

    /// 列出全部未完成组装的快照。
    pub fn list_status(&self, now_ms: u64) -> Vec<AssemblyStatus> {
        self.assemblies
            .values()
            .map(|a| AssemblyStatus {
                key: a.key,
                buffered_bytes: a.buffered_bytes,
                fragment_count: a.fragment_count(),
                has_first_fragment: a.has_first_fragment(),
                has_last_fragment: a.has_last_fragment(),
                contiguous_prefix: a.contiguous_prefix(),
                expected_total: a.last_end,
                age_ms: now_ms.saturating_sub(a.created_at_ms),
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ipv4::{build_ipv4, parse_ipv4, HeaderParams};

    fn key(id: u16) -> FlowKey {
        FlowKey {
            src: Ipv4Addr::new(10, 0, 0, 1),
            dst: Ipv4Addr::new(10, 0, 0, 2),
            protocol: 17,
            identification: id,
        }
    }

    /// 构造测试片。`mf` 对应报文 MF 位（true=还有后续片，false=末片）。
    fn frag(id: u16, offset: usize, data: &[u8], mf: bool) -> FragmentInfo {
        FragmentInfo {
            key: key(id),
            offset,
            payload: data.to_vec(),
            is_last: !mf,
        }
    }

    fn config() -> ReassemblerConfig {
        ReassemblerConfig {
            assembly_ttl_ms: 1000,
            // 测试里禁用插入时的机会式清理，TTL 行为由显式 purge_expired 驱动，
            // 保证时钟确定性。
            purge_interval_ms: u64::MAX,
            ..Default::default()
        }
    }

    /// 把一个完整载荷切成 8 字节对齐的片（最后一片 MF=0）。
    fn split_payload(payload: &[u8], mtu_payload: usize) -> Vec<(usize, Vec<u8>, bool)> {
        assert!(mtu_payload.is_multiple_of(8));
        let mut out = Vec::new();
        let mut off = 0;
        while off < payload.len() {
            let take = mtu_payload.min(payload.len() - off);
            let last = off + take == payload.len();
            // 非末片必须 8 字节对齐；mtu_payload 已对齐。
            out.push((off, payload[off..off + take].to_vec(), !last));
            off += take;
        }
        out
    }

    #[test]
    fn out_of_order_reassembly_matches_original() {
        let payload: Vec<u8> = (0..250).map(|i| (i % 256) as u8).collect();
        let pieces = split_payload(&payload, 40);
        let mut engine = ReassemblyEngine::new(config());

        // 乱序投递：末片、中间片……最后首片。
        let mut order: Vec<usize> = (0..pieces.len()).collect();
        order.reverse();
        let mut got = None;
        for idx in order {
            let (off, data, mf) = &pieces[idx];
            let ev = engine.insert_fragment(frag(1, *off, data, *mf), 0).unwrap();
            if let ReassembleEvent::Completed { data, .. } = ev {
                got = Some(data);
            }
        }
        assert_eq!(got.as_deref(), Some(payload.as_slice()));
        assert_eq!(engine.assembly_count(), 0);
        assert_eq!(engine.bytes_used(), 0);
    }

    #[test]
    fn duplicate_fragments_are_ignored() {
        let mut engine = ReassemblyEngine::new(config());
        engine
            .insert_fragment(frag(5, 0, &[1, 2, 3, 4, 5, 6, 7, 8], true), 0)
            .unwrap();
        // 完全相同的片 -> Duplicate
        let ev = engine
            .insert_fragment(frag(5, 0, &[1, 2, 3, 4, 5, 6, 7, 8], true), 1)
            .unwrap();
        assert!(matches!(ev, ReassembleEvent::Duplicate { .. }));
        assert_eq!(engine.stats.duplicates, 1);
        assert_eq!(engine.bytes_used(), 8); // 不重复计费

        // 末片到达 -> 完成，缓冲归还
        engine
            .insert_fragment(frag(5, 8, &[9, 10], false), 2)
            .unwrap();
        assert_eq!(engine.bytes_used(), 0);
    }

    #[test]
    fn overlapping_fragments_are_rejected_by_default() {
        let mut engine = ReassemblyEngine::new(config());
        engine
            .insert_fragment(frag(7, 0, &[0; 16], true), 0)
            .unwrap();
        // 与 [0,16) 部分重叠 [8,24)
        let err = engine
            .insert_fragment(frag(7, 8, &[1; 16], true), 0)
            .unwrap_err();
        assert!(matches!(
            err,
            ReassembleError::Overlap {
                existing: (0, 16),
                incoming: (8, 24),
                ..
            }
        ));
        // 原组装保留，缓冲字节不变
        assert_eq!(engine.bytes_used(), 16);
        let st = engine.status(&key(7), 0).unwrap();
        assert_eq!(st.fragment_count, 1);

        // 非零偏移的同范围异内容片 -> ConflictingData
        engine
            .insert_fragment(frag(7, 16, &[4; 8], true), 0)
            .unwrap();
        let err = engine
            .insert_fragment(frag(7, 16, &[9; 8], true), 0)
            .unwrap_err();
        assert!(matches!(err, ReassembleError::ConflictingData { .. }));

        // 合法的末片仍可完成重组（[16,24) 用回 [4..] 的旧片内容）
        let data = engine
            .insert_fragment(frag(7, 24, &[2; 8], false), 0)
            .unwrap();
        match data {
            ReassembleEvent::Completed { data, .. } => {
                assert_eq!(data.len(), 32);
                assert!(data[..16].iter().all(|&b| b == 0));
                assert!(data[16..24].iter().all(|&b| b == 4));
                assert!(data[24..].iter().all(|&b| b == 2));
            }
            other => panic!("expected completion, got {other:?}"),
        }
    }

    #[test]
    fn drop_assembly_policy_removes_state_on_overlap() {
        let cfg = ReassemblerConfig {
            overlap_policy: OverlapPolicy::DropAssembly,
            ..config()
        };
        let mut engine = ReassemblyEngine::new(cfg);
        engine
            .insert_fragment(frag(8, 0, &[0; 16], true), 0)
            .unwrap();
        assert!(engine
            .insert_fragment(frag(8, 8, &[1; 16], true), 0)
            .is_err());
        assert_eq!(engine.assembly_count(), 0);
        assert_eq!(engine.bytes_used(), 0); // 字节立即归还预算
    }

    #[test]
    fn missing_first_fragment_buffers_then_completes() {
        let mut engine = ReassemblyEngine::new(config());
        // 先到末片和中间片，缺首片
        engine
            .insert_fragment(frag(9, 16, &[3; 8], false), 0)
            .unwrap();
        engine
            .insert_fragment(frag(9, 8, &[2; 8], true), 0)
            .unwrap();
        let st = engine.status(&key(9), 0).unwrap();
        assert!(!st.has_first_fragment);
        assert!(st.has_last_fragment);
        assert_eq!(st.contiguous_prefix, 0);
        assert_eq!(st.expected_total, Some(24));
        // 首片到达 -> 完成
        let ev = engine
            .insert_fragment(frag(9, 0, &[1; 8], true), 0)
            .unwrap();
        match ev {
            ReassembleEvent::Completed { data, .. } => {
                assert_eq!(
                    data,
                    vec![1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3]
                );
            }
            other => panic!("expected completion, got {other:?}"),
        }
    }

    #[test]
    fn id_reuse_resets_old_assembly() {
        let mut engine = ReassemblyEngine::new(config());
        // 第一轮：id=10 收到首片（缺末片，不完整）
        engine
            .insert_fragment(frag(10, 0, &[0xaa; 8], true), 0)
            .unwrap();
        engine
            .insert_fragment(frag(10, 8, &[0xbb; 8], true), 0)
            .unwrap();
        assert_eq!(engine.bytes_used(), 16);

        // Identification 重用：新的首片到达
        let ev = engine
            .insert_fragment(frag(10, 0, &[0xcc; 8], true), 100)
            .unwrap();
        match ev {
            ReassembleEvent::IdReuseReset {
                discarded_bytes, ..
            } => assert_eq!(discarded_bytes, 16),
            other => panic!("expected IdReuseReset, got {other:?}"),
        }
        assert_eq!(engine.bytes_used(), 8);
        // 新数据报正常完成，内容与新载荷一致
        let ev = engine
            .insert_fragment(frag(10, 8, &[0xdd; 8], false), 100)
            .unwrap();
        match ev {
            ReassembleEvent::Completed { data, .. } => {
                let mut expected = vec![0xcc; 8];
                expected.extend_from_slice(&[0xdd; 8]);
                assert_eq!(data, expected);
            }
            other => panic!("expected completion, got {other:?}"),
        }
        assert_eq!(engine.stats.id_reuse_resets, 1);
    }

    #[test]
    fn timeout_ttl_reclaims_assemblies() {
        let cfg = ReassemblerConfig {
            assembly_ttl_ms: 500,
            purge_interval_ms: u64::MAX,
            ..Default::default()
        };
        let mut engine = ReassemblyEngine::new(cfg);
        engine
            .insert_fragment(frag(11, 0, &[0; 8], true), 0)
            .unwrap();
        engine
            .insert_fragment(frag(12, 0, &[0; 8], true), 0)
            .unwrap();
        assert_eq!(engine.assembly_count(), 2);

        // 400ms：都活着；给 id=11 续命
        let r = engine.purge_expired(400);
        assert_eq!(r.removed_assemblies, 0);
        engine
            .insert_fragment(frag(11, 8, &[1; 8], true), 400)
            .unwrap();

        // 600ms：id=12 已 600ms 无更新 -> 过期；id=11 在 400ms 活跃（age 200ms）-> 存活
        let r = engine.purge_expired(600);
        assert_eq!(r.removed_assemblies, 1);
        assert_eq!(r.reclaimed_bytes, 8);
        assert!(engine.status(&key(11), 600).is_some());
        assert!(engine.status(&key(12), 600).is_none());
        assert_eq!(engine.stats.assemblies_expired, 1);

        // 900ms：id=11 自 400ms 起满 500ms -> 也过期
        let r = engine.purge_expired(900);
        assert_eq!(r.removed_assemblies, 1);
        assert!(engine.status(&key(11), 900).is_none());
        assert_eq!(engine.assembly_count(), 0);
    }

    #[test]
    fn memory_budget_is_enforced() {
        let cfg = ReassemblerConfig {
            mem_budget_bytes: 24,
            ..config()
        };
        let mut engine = ReassemblyEngine::new(cfg);
        engine
            .insert_fragment(frag(20, 0, &[0; 16], true), 0)
            .unwrap();
        // 另一个组装再来 16 字节会超 24
        let err = engine
            .insert_fragment(frag(21, 0, &[0; 16], true), 0)
            .unwrap_err();
        assert!(matches!(
            err,
            ReassembleError::MemoryBudgetExceeded {
                used: 16,
                budget: 24,
                needed: 16
            }
        ));
        // 已存在的组装仍可在预算内继续
        engine
            .insert_fragment(frag(20, 16, &[0; 8], false), 0)
            .unwrap();
        assert_eq!(engine.bytes_used(), 0); // 完成后归还
    }

    #[test]
    fn max_datagram_limit_is_enforced() {
        let cfg = ReassemblerConfig {
            max_datagram_bytes: 24,
            ..config()
        };
        let mut engine = ReassemblyEngine::new(cfg);
        engine
            .insert_fragment(frag(22, 0, &[0; 16], true), 0)
            .unwrap();
        // 末片声明总长度 32 -> 超 24
        let err = engine
            .insert_fragment(frag(22, 24, &[0; 8], false), 0)
            .unwrap_err();
        assert!(matches!(
            err,
            ReassembleError::DatagramTooLarge { attempted: 32, .. }
        ));
    }

    #[test]
    fn conflicting_last_fragments_are_rejected() {
        let mut engine = ReassemblyEngine::new(config());
        engine
            .insert_fragment(frag(23, 0, &[0; 8], true), 0)
            .unwrap();
        engine
            .insert_fragment(frag(23, 8, &[0; 8], false), 0) // 末片 end=16，完成并移除
            .unwrap();
        // 构造一个未完成场景：先收到末片 end=24，再收到另一个末片 end=16
        let mut engine = ReassemblyEngine::new(config());
        engine
            .insert_fragment(frag(24, 16, &[0; 8], false), 0)
            .unwrap();
        let err = engine
            .insert_fragment(frag(24, 24, &[0; 8], false), 0)
            .unwrap_err();
        assert!(matches!(
            err,
            ReassembleError::ConflictingLastFragment {
                previous_end: 24,
                new_end: 32,
                ..
            }
        ));
    }

    #[test]
    fn full_path_through_ipv4_parse_and_reassembly() {
        // 端到端：构造 -> 线格式字节 -> parse_ipv4 -> 重组 -> 对照原始载荷
        let payload: Vec<u8> = (0..100).map(|i| (i * 7 % 256) as u8).collect();
        let pieces = split_payload(&payload, 24);
        let raws: Vec<Vec<u8>> = pieces
            .iter()
            .map(|(off, data, mf)| {
                build_ipv4(&HeaderParams {
                    src: Ipv4Addr::new(192, 168, 1, 10),
                    dst: Ipv4Addr::new(192, 168, 1, 20),
                    protocol: 6,
                    identification: 0x4242,
                    more_fragments: *mf,
                    fragment_offset: *off,
                    payload: data.clone(),
                })
            })
            .collect();

        let mut engine = ReassemblyEngine::new(config());
        let mut got = None;
        // 乱序
        for i in (0..raws.len()).rev() {
            let pkt = parse_ipv4(&raws[i]).unwrap();
            if let ReassembleEvent::Completed { data, .. } = engine.insert_packet(&pkt, 0).unwrap()
            {
                got = Some(data);
            }
        }
        assert_eq!(got.as_deref(), Some(payload.as_slice()));
    }

    #[test]
    fn non_fragment_packet_passes_through() {
        let mut engine = ReassemblyEngine::new(config());
        let raw = build_ipv4(&HeaderParams {
            src: Ipv4Addr::new(1, 1, 1, 1),
            dst: Ipv4Addr::new(2, 2, 2, 2),
            protocol: 1,
            identification: 1,
            more_fragments: false,
            fragment_offset: 0,
            payload: b"hello".to_vec(),
        });
        let pkt = parse_ipv4(&raw).unwrap();
        let ev = engine.insert_packet(&pkt, 0).unwrap();
        match ev {
            ReassembleEvent::Completed { data, .. } => assert_eq!(data, b"hello"),
            other => panic!("expected completion, got {other:?}"),
        }
        assert_eq!(engine.assembly_count(), 0);
    }
}
