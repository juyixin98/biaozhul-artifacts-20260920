//! 错误类型。
//!
//! 对“桶满且无法分裂”的情况给出明确错误（HTTP 507 Insufficient Storage），
//! 其余可预期错误映射到 4xx/500。

use std::fmt;

/// 索引操作错误。
#[derive(Debug)]
pub enum IndexError {
    /// 键超过页面允许的最大字节数。
    KeyTooLong { len: usize, max: usize },
    /// 值超过页面允许的最大字节数。
    ValueTooLong { len: usize, max: usize },
    /// 键为空。
    EmptyKey,
    /// 桶已满且无法通过分裂腾出空间：
    /// - 分裂后所有记录仍然落在同一个桶（哈希全碰撞），或
    /// - 全局深度已达上限。
    BucketCapacityExhausted { bucket: u32, reason: &'static str },
    /// 键不存在（GET/DELETE 语义错误）。
    NotFound,
    /// 磁盘格式损坏 / 校验和不匹配。
    Corrupt(String),
    /// 打开已有文件时，命令行请求的哈希模式与文件中记录的不一致。
    HashModeMismatch { file: String, requested: String },
    /// 底层 IO 错误。
    Io(std::io::Error),
}

impl fmt::Display for IndexError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            IndexError::KeyTooLong { len, max } => {
                write!(f, "键长度 {len} 字节超过上限 {max} 字节")
            }
            IndexError::ValueTooLong { len, max } => {
                write!(f, "值长度 {len} 字节超过上限 {max} 字节")
            }
            IndexError::EmptyKey => write!(f, "键不能为空"),
            IndexError::BucketCapacityExhausted { bucket, reason } => {
                write!(f, "桶 {bucket} 容量耗尽且无法分裂：{reason}")
            }
            IndexError::NotFound => write!(f, "键不存在"),
            IndexError::Corrupt(s) => write!(f, "索引文件损坏：{s}"),
            IndexError::HashModeMismatch { file, requested } => write!(
                f,
                "哈希模式不匹配：文件记录为 {file}，启动参数请求 {requested}"
            ),
            IndexError::Io(e) => write!(f, "IO 错误：{e}"),
        }
    }
}

impl std::error::Error for IndexError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            IndexError::Io(e) => Some(e),
            _ => None,
        }
    }
}

impl From<std::io::Error> for IndexError {
    fn from(e: std::io::Error) -> Self {
        IndexError::Io(e)
    }
}

pub type Result<T> = std::result::Result<T, IndexError>;
