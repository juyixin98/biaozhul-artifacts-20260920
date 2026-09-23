//! sst — 前缀压缩有序表（只读 SSTable 风格存储）。
//!
//! 磁盘格式（全部小端）：
//!
//! ```text
//! +----------------+  <-- 0
//! | data block 0   |
//! | ...            |
//! | data block N-1 |
//! | index block    |  <-- footer.index_offset
//! | footer (32B)   |  <-- file_len - 32
//! +----------------+
//! ```
//!
//! 详见 README.md 的格式规范与同步边界说明。

pub mod block;
pub mod crc32;
pub mod error;
pub mod hex;
pub mod storage;
pub mod table;
pub mod varint;

pub use error::{Error, Result};
pub use storage::{FaultyWriter, FileReader, FileWriter, MemStorage, ReadAt, WriteSink};
pub use table::{
    TableOptions, TableReader, TableStats, TableWriter, VerifyReport, FOOTER_SIZE, MAGIC,
};
