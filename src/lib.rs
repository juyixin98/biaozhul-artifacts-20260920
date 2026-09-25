//! # ipfrag —— 离线 IPv4 分片重组
//!
//! 分层：
//!
//! * [`parser`]    —— 从零实现的增量字节解析原语（子集 / 长度上限 / 明确错误类型）；
//! * [`ipv4`]      —— 手写的 IPv4 报文最小解析与测试构造器（不使用现成协议库）；
//! * [`reassembly`] —— 按 (src,dst,proto,id) 分组的离线分片重组引擎，
//!   含明确的重叠片拒绝策略、组装寿命（TTL）与内存预算；
//! * [`sha256`]    —— 零依赖 SHA-256（载荷指纹）；
//! * [`json`]      —— 零依赖最小 JSON；
//! * [`server`]    —— 本地 TCP 测试服务（JSON 行协议，无需特权）。
//!
//! 附带二进制：`ipfrag-server` / `ipfrag-client` / `fraggen`。

pub mod ipv4;
pub mod json;
pub mod parser;
pub mod reassembly;
pub mod server;
pub mod sha256;

pub use ipv4::{
    build_ipv4, ones_complement_checksum, parse_ipv4, parse_ipv4_inc, HeaderParams, Ipv4Packet,
};
pub use parser::{ByteStream, Cursor, ParseError, ParseResult};
pub use reassembly::{
    AssemblyStatus, FlowKey, FragmentInfo, OverlapPolicy, PurgeReport, ReassembleError,
    ReassembleEvent, ReassemblerConfig, ReassemblyEngine, Stats,
};
pub use sha256::{sha256, sha256_hex};
