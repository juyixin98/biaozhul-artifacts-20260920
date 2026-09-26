//! 规范霍夫曼编码（Canonical Huffman）纯后端库。
//!
//! 模块划分：
//! - [`error`]：错误类型（所有格式违例都有独立种类，便于调用方区分）
//! - [`bits`]：MSB-first 位流读写
//! - [`huffman`]：确定性建树、码长计算、规范码生成、解码前缀树
//! - [`format`]：`.chfc` 分块容器格式
//! - [`stream`]：有界流式压缩 / 解压
//! - [`jsonapi`]：JSON 控制入口（请求解析、响应序列化）

pub mod bits;
pub mod error;
pub mod format;
pub mod huffman;
pub mod jsonapi;
pub mod stream;

pub use error::Error;
pub use format::{BlockHeader, MAGIC, VERSION};
pub use huffman::{
    build_canonical_codes, build_decode_trie, build_lengths, frequencies, CodeBook, DecodeTable,
    LengthTable,
};
pub use stream::{
    compress_bytes, compress_stream, decompress_bytes, decompress_stream, CompressStats,
    DecompressStats, IncompletePolicy, Limits, DEFAULT_BLOCK_SIZE, DEFAULT_MAX_BLOCK_BYTES,
    DEFAULT_MAX_OUTPUT_BYTES,
};
