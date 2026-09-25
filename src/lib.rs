//! # ipfrag —— 离线 IPv4 分片重组库
//!
//! 纯后端、零三方依赖（只用 `std`）。包含三个核心模块：
//!
//! * [`byte_reader`]：手写的**增量字节解析库**。`Reader` 支持受限子集
//!   （subset：从任意切片起点构造、带剩余长度上限）、长度上限校验和
//!   显式分类的错误类型；`FeedBuffer` 支持字节流随到随喂、不完整时
//!   报 [`Incomplete`](byte_reader::Incomplete) 而非越界。
//! * [`ipv4`]：用上述解析库手工解析 IPv4 头部（不使用任何现成协议
//!   解析器），并提供报文构造/校验和工具用于测试。
//! * [`reassembly`]：离线 IPv4 分片重组引擎。按
//!   (源地址, 目的地址, 协议号, 标识 ID) 四元组分组；显式的重叠片
//!   **拒绝**策略、寿命（TTL）过期清理与总内存预算。
//!
//! [`server`] 模块提供一个基于 `std::net` 的本地 TCP 测试服务，
//! 报文格式为换行分隔的 JSON（JSON 解析同样为手写）。
//!
//! ## 概要示例
//!
//! ```
//! use ipfrag::reassembly::{Reassembler, Config};
//!
//! # fn main() -> Result<(), Box<dyn std::error::Error>> {
//! let mut engine = Reassembler::new(Config {
//!     fragment_ttl_ms: 30_000,
//!     total_memory_budget: 1 << 20,
//!     max_datagram_payload: 65_535,
//! });
//!
//! // 两片、乱序到达：先到偏移 1200 的尾片，再到偏移 0 的首片。
//! let tail = ipfrag::ipv4::build_fragment((192,168,0,1), (10,0,0,200), 17, 0x1234,
//!                                         1200, false, vec![0xBB; 83]);
//! let head = ipfrag::ipv4::build_fragment((192,168,0,1), (10,0,0,200), 17, 0x1234,
//!                                         0, true, vec![0xAA; 1200]);
//! assert!(engine.add_packet(&tail, 0)?.is_pending());
//! let done = engine.add_packet(&head, 10)?.into_completed().unwrap();
//! assert_eq!(done.payload.len(), 1283);
//! # Ok(()) }
//! ```

pub mod byte_reader;
pub mod ipv4;
pub mod reassembly;
pub mod server;

pub use reassembly::{AddResult, Config, Key, Reassembler, ReassemblyError};
