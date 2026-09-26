//! 流式 LZ77 编码器（贪心 + 哈希链匹配，内存有界）。
//!
//! 内存上界：窗口历史（≤ window）+ 未决尾部（< MAX_MATCH）+ 哈希表
//! （2^15 个头指针 + window 个链节点），与输入总长度无关。

use crate::error::{Error, Result};
use crate::format::*;

const HASH_BITS: u32 = 15;
const HASH_SIZE: usize = 1 << HASH_BITS;
const NIL: i64 = -1;
/// 触发 buf 前端裁剪的冗余量，摊薄 drain 成本。
const TRIM_SLACK: u64 = 4096;

/// 编码器配置。
#[derive(Clone, Copy, Debug)]
pub struct EncoderConfig {
    /// 滑动窗口大小（最大回距），1..=65535。
    pub window: usize,
    /// 每个位置的哈希链最大搜索深度。
    pub max_chain: usize,
}

impl Default for EncoderConfig {
    fn default() -> Self {
        EncoderConfig {
            window: DEFAULT_WINDOW,
            max_chain: 32,
        }
    }
}

/// 流式编码器。通过 [`Encoder::feed`] 分块喂入，[`Encoder::finish`] 收尾。
pub struct Encoder {
    cfg: EncoderConfig,
    /// 最近输入字节：`buf[0]` 对应绝对位置 `base`。
    buf: Vec<u8>,
    base: u64,
    /// 下一个待编码的绝对位置。
    pos: u64,
    /// 已入哈希表的位置前缀：[0, inserted_up_to) 均已入表。
    /// 统一在编码位置 p 前把 <p 的位置全部入表，保证表内容与分块方式无关。
    inserted_up_to: u64,
    /// 待冲刷字面量段的起点（绝对位置）。
    lit_start: u64,
    /// 哈希头表：3 字节哈希 -> 最新绝对位置。
    heads: Vec<i64>,
    /// 链节点：`prev[p % window]` = 前一个同哈希位置。
    prev: Vec<i64>,
    header_written: bool,
    finished: bool,
    tokens: u64,
}

impl Encoder {
    pub fn new(cfg: EncoderConfig) -> Result<Self> {
        if cfg.window == 0 || cfg.window > MAX_WINDOW {
            return Err(Error::InvalidWindow(cfg.window));
        }
        Ok(Encoder {
            cfg,
            buf: Vec::new(),
            base: 0,
            pos: 0,
            inserted_up_to: 0,
            lit_start: 0,
            heads: vec![NIL; HASH_SIZE],
            prev: vec![NIL; cfg.window],
            header_written: false,
            finished: false,
            tokens: 0,
        })
    }

    /// 喂入一块输入，编码结果追加到 `out`。
    pub fn feed(&mut self, input: &[u8], out: &mut Vec<u8>) -> Result<()> {
        if self.finished {
            return Err(Error::EncoderFinished);
        }
        self.write_header(out);
        self.buf.extend_from_slice(input);
        self.process(false, out);
        self.trim();
        Ok(())
    }

    /// 收尾：编码剩余尾部并冲刷字面量。返回 token 总数。
    pub fn finish(mut self, out: &mut Vec<u8>) -> Result<u64> {
        if self.finished {
            return Err(Error::EncoderFinished);
        }
        self.finished = true;
        self.write_header(out);
        self.process(true, out);
        self.flush_literals(out);
        Ok(self.tokens)
    }

    fn write_header(&mut self, out: &mut Vec<u8>) {
        if self.header_written {
            return;
        }
        self.header_written = true;
        out.extend_from_slice(MAGIC);
        out.push(VERSION);
        out.push(0); // flags
        out.extend_from_slice(&(self.cfg.window as u16).to_le_bytes());
        out.extend_from_slice(&(MAX_MATCH as u16).to_le_bytes());
        out.extend_from_slice(&[0; 2]); // reserved
    }

    /// 编码尽可能多的已缓冲字节。
    ///
    /// 非收尾时保留 `MAX_MATCH - 1` 字节尾部不编码：从该尾部开始的匹配
    /// 可能被后续 feed 的数据延长，提前定码会破坏分块等价性。
    fn process(&mut self, finishing: bool, out: &mut Vec<u8>) {
        loop {
            let end = self.base + self.buf.len() as u64;
            let avail = (end - self.pos) as usize;
            let holdback = if finishing { 0 } else { MAX_MATCH - 1 };
            if avail <= holdback {
                break;
            }
            // 编码 pos 前，把 <pos 的可哈希位置统一入表（与分块无关的不变量）
            while self.inserted_up_to < self.pos && self.inserted_up_to + MIN_MATCH as u64 <= end {
                self.insert(self.inserted_up_to);
                self.inserted_up_to += 1;
            }
            let max_len = avail.min(MAX_MATCH);
            let (mut best_len, mut best_dist) = (0usize, 0usize);
            if max_len >= MIN_MATCH {
                let mut cand = self.heads[self.hash_at(self.pos)];
                let mut steps = 0;
                while cand >= 0 && steps < self.cfg.max_chain {
                    let dist = self.pos - cand as u64;
                    if dist == 0 || dist > self.cfg.window as u64 {
                        break; // 链按新到旧排列，更远的不必再看
                    }
                    let l = self.match_len(cand as u64, self.pos, max_len);
                    if l > best_len {
                        best_len = l;
                        best_dist = dist as usize;
                        if l >= max_len {
                            break;
                        }
                    }
                    let next = self.prev[(cand as u64 % self.cfg.window as u64) as usize];
                    if next < 0 || next >= cand {
                        break; // 槽位被更新位置覆盖，链到此为止
                    }
                    cand = next;
                    steps += 1;
                }
            }
            if best_len >= MIN_MATCH {
                self.flush_literals(out);
                self.emit_match(best_dist, best_len, out);
                self.pos += best_len as u64;
                self.lit_start = self.pos;
            } else {
                self.pos += 1;
                if self.pos - self.lit_start >= MAX_LITERAL_RUN as u64 {
                    self.flush_literals(out);
                }
            }
        }
    }

    fn hash_at(&self, p: u64) -> usize {
        let i = (p - self.base) as usize;
        ((self.buf[i] as usize) << 10 ^ (self.buf[i + 1] as usize) << 5 ^ self.buf[i + 2] as usize)
            & (HASH_SIZE - 1)
    }

    fn insert(&mut self, p: u64) {
        if p + MIN_MATCH as u64 > self.base + self.buf.len() as u64 {
            return; // 不足 3 字节可哈希（仅收尾阶段的尾部）
        }
        let h = self.hash_at(p);
        let slot = (p % self.cfg.window as u64) as usize;
        self.prev[slot] = self.heads[h];
        self.heads[h] = p as i64;
    }

    fn match_len(&self, cand: u64, pos: u64, max: usize) -> usize {
        let ic = (cand - self.base) as usize;
        let ip = (pos - self.base) as usize;
        let mut n = 0;
        while n < max && self.buf[ic + n] == self.buf[ip + n] {
            n += 1;
        }
        n
    }

    fn flush_literals(&mut self, out: &mut Vec<u8>) {
        while self.lit_start < self.pos {
            let n = ((self.pos - self.lit_start) as usize).min(MAX_LITERAL_RUN);
            out.push((n - 1) as u8);
            let s = (self.lit_start - self.base) as usize;
            out.extend_from_slice(&self.buf[s..s + n]);
            self.lit_start += n as u64;
            self.tokens += 1;
        }
    }

    fn emit_match(&mut self, dist: usize, len: usize, out: &mut Vec<u8>) {
        debug_assert!((MIN_MATCH..=MAX_MATCH).contains(&len));
        debug_assert!(dist >= 1 && dist <= self.cfg.window);
        out.push(0x80 | (len - MIN_MATCH) as u8);
        out.extend_from_slice(&(dist as u16).to_le_bytes());
        self.tokens += 1;
    }

    /// 丢弃不再需要的字节：匹配历史只需 pos-window 起，
    /// 但尚未冲刷的字面量段（lit_start 起）也必须保留。
    fn trim(&mut self) {
        let keep_from = self
            .pos
            .saturating_sub(self.cfg.window as u64)
            .min(self.lit_start);
        if keep_from > self.base + TRIM_SLACK {
            let drop = (keep_from - self.base) as usize;
            self.buf.drain(..drop);
            self.base = keep_from;
        }
    }
}
