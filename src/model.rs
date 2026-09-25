//! order-0 自适应频率模型。
//!
//! 规则固定（与 FORMAT.md 一致，编解码双方必须严格同步）：
//! - 符号表：0..=255 对应字节 0x00..=0xFF，256 为 EOF 终止符，共 257 个符号。
//! - 初始频率：所有符号均为 1，总频率 257。
//! - 更新：每处理一个符号（含 EOF），其频率加 1。
//! - 重标定：总频率达到 [`MAX_TOTAL_FREQ`] 时，所有频率折半并向上取整
//!   （`f = (f + 1) / 2`，保证最小为 1），总频率重新求和。

/// EOF 终止符的符号编号。
pub const EOF_SYMBOL: u16 = 256;
/// 符号总数：256 个字节符号 + 1 个 EOF。
pub const NUM_SYMBOLS: usize = 257;
/// 触发重标定的总频率上限。远小于区间精度约束（2^30），保证整数运算不溢出。
pub const MAX_TOTAL_FREQ: u32 = 16383;

/// order-0 自适应模型。编码器与解码器各自持有并同步更新。
#[derive(Debug, Clone)]
pub struct AdaptiveModel {
    freq: [u32; NUM_SYMBOLS],
    total: u32,
    rescale_count: u64,
}

impl Default for AdaptiveModel {
    fn default() -> Self {
        Self::new()
    }
}

impl AdaptiveModel {
    pub fn new() -> Self {
        Self {
            freq: [1; NUM_SYMBOLS],
            total: NUM_SYMBOLS as u32,
            rescale_count: 0,
        }
    }

    /// 当前总频率。
    pub fn total(&self) -> u32 {
        self.total
    }

    /// 已发生的重标定次数。
    pub fn rescale_count(&self) -> u64 {
        self.rescale_count
    }

    /// 符号 `sym` 的当前频率（恒 >= 1）。
    pub fn freq_of(&self, sym: u16) -> u32 {
        self.freq[sym as usize]
    }

    /// 符号 `sym` 的累计频率下界，即 freq[0] + ... + freq[sym-1]。
    pub fn cum_freq_of(&self, sym: u16) -> u32 {
        self.freq[..sym as usize].iter().sum()
    }

    /// 找到累计频率区间包含 `value` 的符号：
    /// 返回 (符号, 累计下界, 累计上界)，满足 下界 <= value < 上界。
    pub fn find_symbol(&self, value: u32) -> (u16, u32, u32) {
        debug_assert!(value < self.total);
        let mut cum = 0u32;
        for (sym, &f) in self.freq.iter().enumerate() {
            let next = cum + f;
            if value < next {
                return (sym as u16, cum, next);
            }
            cum = next;
        }
        unreachable!("value < total 保证必命中某个符号");
    }

    /// 处理一个符号后更新模型；总频率达到上限时重标定。
    pub fn update(&mut self, sym: u16) {
        self.freq[sym as usize] += 1;
        self.total += 1;
        if self.total >= MAX_TOTAL_FREQ {
            self.rescale();
        }
    }

    /// 固定重标定规则：所有频率折半向上取整（最小为 1），总频率重算。
    fn rescale(&mut self) {
        let mut total = 0u32;
        for f in self.freq.iter_mut() {
            *f = (*f + 1) >> 1;
            total += *f;
        }
        self.total = total;
        self.rescale_count += 1;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn initial_state() {
        let m = AdaptiveModel::new();
        assert_eq!(m.total(), NUM_SYMBOLS as u32);
        assert_eq!(m.freq_of(0), 1);
        assert_eq!(m.freq_of(EOF_SYMBOL), 1);
        assert_eq!(m.cum_freq_of(EOF_SYMBOL), 256);
        assert_eq!(m.rescale_count(), 0);
    }

    #[test]
    fn update_increments_and_finds_symbol() {
        let mut m = AdaptiveModel::new();
        m.update(7);
        assert_eq!(m.freq_of(7), 2);
        assert_eq!(m.total(), NUM_SYMBOLS as u32 + 1);
        // 初始 cum: 符号 7 覆盖 [7, 9)
        let (sym, lo, hi) = m.find_symbol(7);
        assert_eq!((sym, lo, hi), (7, 7, 9));
        let (sym, lo, hi) = m.find_symbol(8);
        assert_eq!((sym, lo, hi), (7, 7, 9));
        let (sym, _, _) = m.find_symbol(9);
        assert_eq!(sym, 8);
    }

    #[test]
    fn rescale_halves_with_floor_one() {
        let mut m = AdaptiveModel::new();
        // 把符号 0 反复更新，期间必然多次触发重标定
        // （update 在 total >= MAX_TOTAL_FREQ 时立即 rescale，故 total 恒 < MAX）
        for _ in 0..(MAX_TOTAL_FREQ * 2) {
            m.update(0);
        }
        assert!(m.rescale_count() >= 1);
        assert!(m.total() < MAX_TOTAL_FREQ);
        assert!(m.freq_of(0) >= 1);
        // 所有频率 >= 1
        for s in 0..NUM_SYMBOLS as u16 {
            assert!(m.freq_of(s) >= 1);
        }
        // 总频率与频率表一致
        let sum: u32 = (0..NUM_SYMBOLS as u16).map(|s| m.freq_of(s)).sum();
        assert_eq!(sum, m.total());
    }

    #[test]
    fn long_skewed_stream_rescales_many_times() {
        let mut m = AdaptiveModel::new();
        for _ in 0..300_000 {
            m.update(0);
        }
        assert!(m.rescale_count() >= 2, "rescale_count={}", m.rescale_count());
    }
}
