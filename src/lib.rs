//! # brpc-reuse — binary RPC multiplexing over TCP (pure std implementation)
//!
//! Modules:
//! - [`frame`]: wire format, CRC-32, encoder and the **incremental byte
//!   parser** (partial packets / sticky packets / bounded buffering).
//! - [`error`]: explicit error taxonomy, including fatal framing errors vs
//!   recoverable per-request protocol errors.
//! - [`sync`]: bounded MPSC channel, counting semaphore, cancellation token.
//! - [`server`]: multiplexing TCP server with bounded in-flight concurrency.
//! - [`client`]: multiplexing client with monotonic non-reused request ids,
//!   timeout/cancel handling and late-response detection.
//! - [`service`]: the tiny demo/test application protocol used end-to-end.

pub mod client;
pub mod error;
pub mod frame;
pub mod server;
pub mod service;
pub mod sync;

pub use client::{Call, ClientConfig, Connection, LateReport};
pub use error::{ErrorCode, ParseError, RpcError};
pub use frame::{
    crc32, encode_frame, encode_frame_vec, Command, Frame, IncrementalDecoder, DEFAULT_MAX_PAYLOAD,
    HEADER_LEN, TRAILER_LEN,
};
pub use server::{RunningServer, ServerConfig};
pub use service::{ServiceRequest, ServiceResponse};
