//! Error types shared by the index core and the HTTP layer.
use std::fmt;

#[derive(Debug)]
pub enum IndexError {
    /// Bucket is full and every stored key plus the new key hash to the same
    /// value: no number of directory doublings / bucket splits can ever make
    /// them separable.
    CollisionCapacity {
        key: Vec<u8>,
        hash: u64,
        bucket_capacity: usize,
    },
    /// Bucket is full and all keys share the same prefix up to the configured
    /// maximum global depth (`2^max_depth` directory slots exhausted).
    DepthLimit {
        max_depth: u32,
        bucket_capacity: usize,
    },
    /// A key or value exceeds the per-page limits declared at index creation.
    EntryTooLarge {
        what: &'static str,
        size: usize,
        limit: usize,
    },
    /// A bucket on disk cannot hold `bucket_capacity` entries of the configured
    /// maximum size. This is a configuration error reported at creation/open.
    Config(&'static str),
    /// Persistent file / fsync failure.
    Io(std::io::Error),
    /// File contents do not match this implementation's format.
    Corrupt(String),
}

impl fmt::Display for IndexError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            IndexError::CollisionCapacity {
                key,
                hash,
                bucket_capacity,
            } => write!(
                f,
                "capacity exhausted: bucket full ({} entries) and key {:?} collides with all stored keys at hash {:#018x}; no split can separate them",
                bucket_capacity,
                String::from_utf8_lossy(key),
                hash,
            ),
            IndexError::DepthLimit {
                max_depth,
                bucket_capacity,
            } => write!(
                f,
                "capacity exhausted: bucket full ({} entries) and global depth limit {} reached ({} directory slots)",
                bucket_capacity,
                max_depth,
                1usize << max_depth,
            ),
            IndexError::EntryTooLarge { what, size, limit } => write!(
                f,
                "{} too large: {} bytes, page limit is {} bytes",
                what, size, limit
            ),
            IndexError::Config(msg) => write!(f, "configuration error: {}", msg),
            IndexError::Io(e) => write!(f, "io error: {}", e),
            IndexError::Corrupt(msg) => write!(f, "corrupt index file: {}", msg),
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
