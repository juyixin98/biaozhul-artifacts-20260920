//! 段内去重字符串字典。
//!
//! 自行实现，不依赖 `std::collections::HashMap`：
//! - 哈希：FNV-1a 64 位；
//! - 冲突解决：开放寻址 + 线性探测；
//! - 容量：2 的幂，负载因子上限约 7/8，满则倍增重排。
//!
//! 字典按**首次插入顺序**为每个不同字符串分配从 0 开始的 ID。
//! 空字符串是合法条目，与 NULL 无关（NULL 在行数据中用独立标志表示，不进字典）。

/// 初始桶数（2 的幂）。
const INIT_CAP: usize = 8;

/// 去重字符串字典。
#[derive(Debug, Clone)]
pub struct DictTable {
    /// 键：字符串字节（均为合法 UTF-8）。
    keys: Vec<Vec<u8>>,
    /// 哈希表：桶内存的是 keys 的索引 +1，0 表示空槽。
    /// 用 u32：单段字典上限约 42 亿条，且受内存限制实际远小于此。
    buckets: Vec<u32>,
    /// 掩码 = 桶数 - 1。
    mask: usize,
}

fn fnv1a(data: &[u8]) -> u64 {
    const OFFSET: u64 = 0xcbf2_9ce4_8422_b325;
    const PRIME: u64 = 0x0000_0100_0000_01b3;
    let mut h = OFFSET;
    for &b in data {
        h ^= u64::from(b);
        h = h.wrapping_mul(PRIME);
    }
    h
}

impl Default for DictTable {
    fn default() -> Self {
        Self::new()
    }
}

impl DictTable {
    /// 创建空字典。
    pub fn new() -> Self {
        DictTable {
            keys: Vec::new(),
            buckets: vec![0u32; INIT_CAP],
            mask: INIT_CAP - 1,
        }
    }

    /// 字典条目数（不含 NULL）。
    pub fn len(&self) -> usize {
        self.keys.len()
    }

    /// 字典是否为空。
    pub fn is_empty(&self) -> bool {
        self.keys.is_empty()
    }

    /// 返回第 `id` 个条目的字节。id 越界返回 None。
    pub fn get(&self, id: u64) -> Option<&[u8]> {
        let id = id as usize;
        self.keys.get(id).map(Vec::as_slice)
    }

    /// 按值查找已有 ID；不存在返回 None。
    pub fn find(&self, value: &[u8]) -> Option<u64> {
        let h = fnv1a(value) as usize;
        let mut slot = h & self.mask;
        loop {
            let entry = self.buckets[slot];
            if entry == 0 {
                return None;
            }
            let idx = (entry - 1) as usize;
            if self.keys[idx].as_slice() == value {
                return Some(idx as u64);
            }
            slot = (slot + 1) & self.mask;
        }
    }

    /// 插入字符串；已存在则返回旧 ID，否则分配新 ID（等于插入前条目数）。
    pub fn intern(&mut self, value: &[u8]) -> u64 {
        if let Some(id) = self.find(value) {
            return id;
        }
        // 负载因子 >= 7/8 时扩容。
        if self.keys.len() + 1 >= self.buckets.len() - (self.buckets.len() >> 3) {
            self.grow();
        }
        let id = self.keys.len() as u64;
        self.keys.push(value.to_vec());
        let h = fnv1a(value) as usize;
        let mut slot = h & self.mask;
        loop {
            if self.buckets[slot] == 0 {
                self.buckets[slot] = (id as u32) + 1;
                return id;
            }
            slot = (slot + 1) & self.mask;
        }
    }

    fn grow(&mut self) {
        let new_len = self.buckets.len() * 2;
        let mut buckets = vec![0u32; new_len];
        let mask = new_len - 1;
        for (idx, key) in self.keys.iter().enumerate() {
            let h = fnv1a(key) as usize;
            let mut slot = h & mask;
            loop {
                if buckets[slot] == 0 {
                    buckets[slot] = (idx as u32) + 1;
                    break;
                }
                slot = (slot + 1) & mask;
            }
        }
        self.buckets = buckets;
        self.mask = mask;
    }

    /// 按 ID 顺序遍历所有条目。
    pub fn iter(&self) -> impl Iterator<Item = &[u8]> {
        self.keys.iter().map(Vec::as_slice)
    }

    /// 只读键表（供合并模块直接消费有序条目）。
    pub fn keys(&self) -> &[Vec<u8>] {
        &self.keys
    }

    /// 忽略哈希结构、直接以有序条目构建字典（供解码/测试使用）。
    /// 若出现重复值则返回 [`Error::DuplicateDictEntry`]。
    pub fn from_ordered_entries(entries: Vec<Vec<u8>>) -> crate::error::Result<Self> {
        let mut table = DictTable::new();
        for e in &entries {
            if table.find(e).is_some() {
                return Err(crate::error::Error::DuplicateDictEntry);
            }
            // 直接走 intern 会再哈希一次，但保证结构一致。
            table.intern(e);
        }
        Ok(table)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_seen_order_ids() {
        let mut t = DictTable::new();
        assert_eq!(t.intern(b"banana"), 0);
        assert_eq!(t.intern(b"apple"), 1);
        assert_eq!(t.intern(b"banana"), 0);
        assert_eq!(t.intern(b""), 2);
        assert_eq!(t.intern(b""), 2);
        assert_eq!(t.len(), 3);
        assert_eq!(t.get(1), Some(b"apple".as_slice()));
    }

    #[test]
    fn grow_keeps_lookup_correct() {
        let mut t = DictTable::new();
        let n = 500u64;
        for i in 0..n {
            let s = format!("v{i}");
            assert_eq!(t.intern(s.as_bytes()), i);
        }
        for i in 0..n {
            let s = format!("v{i}");
            assert_eq!(t.find(s.as_bytes()), Some(i));
        }
        assert_eq!(t.len(), n as usize);
    }

    #[test]
    fn unicode_keys_distinct() {
        let mut t = DictTable::new();
        assert_eq!(t.intern("你好".as_bytes()), 0);
        assert_eq!(t.intern("こんにちは".as_bytes()), 1);
        assert_eq!(t.intern("你好".as_bytes()), 0);
    }

    #[test]
    fn reject_duplicates_from_entries() {
        let entries = vec![b"a".to_vec(), b"a".to_vec()];
        assert!(DictTable::from_ordered_entries(entries).is_err());
    }
}
