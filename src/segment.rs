use serde::{Deserialize, Serialize};

/// 一条写入记录。`value = None` 表示墓碑（删除标记）。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Entry {
    pub key: String,
    /// 全局单调递增版本号，越大越新。
    pub seq: u64,
    /// None = tombstone
    pub value: Option<String>,
}

impl Entry {
    pub fn new(key: impl Into<String>, seq: u64, value: Option<String>) -> Self {
        Self {
            key: key.into(),
            seq,
            value,
        }
    }

    pub fn is_tombstone(&self) -> bool {
        self.value.is_none()
    }
}
