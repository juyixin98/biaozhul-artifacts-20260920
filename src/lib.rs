//! sse-resume：手写 SSE（Server-Sent Events）增量字节解析库 + 本地 TCP 测试服务。
//!
//! 模块划分：
//! - [`parser`]：客户端侧增量解码（逐字节喂入，跨块安全）。
//! - [`encoder`]：服务端侧事件编码。
//! - [`history`]：有限历史缓冲，支持 Last-Event-ID 续传与过期游标重置。
//! - [`server`]：本地 TCP 测试服务（极简 HTTP/1.1 握手 + SSE 流）。
//! - [`client`]：客户端连接与自动重连续传逻辑。

pub mod client;
pub mod encoder;
pub mod history;
pub mod parser;
pub mod server;
