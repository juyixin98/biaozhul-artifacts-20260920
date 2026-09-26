//! # ecstripe
//!
//! A small, self-contained **erasure-coded stripe recovery** library.
//!
//! It implements a systematic Reed-Solomon code over GF(2^8) on top of the
//! mature [`gf256`] finite-field crate: the field arithmetic (multiplication
//! modulo `0x11d`, primitive element `0x02`) comes from the crate, while the
//! coding-matrix construction, Gauss-Jordan inversion, encoding and recovery
//! are implemented in [`coding`].
//!
//! Data moves through a documented streaming binary container ([`container`])
//! with bounded memory usage and bounded decoded output length ([`codec`]).
//! The binary entry point (`src/main.rs`) is driven entirely by JSON requests
//! on stdin and emits JSON responses on stdout.
//!
//! ## Scope of the recovery guarantee
//!
//! Recovery succeeds for **known erasures when the number of missing shards
//! does not exceed the number of parity shards**. Missing more shards,
//! inconsistent shard lengths and framing damage are reported as errors.
//! Present-but-corrupted shards are *not* detected: callers must verify data
//! externally (the container optionally carries a plaintext SHA-256 for this
//! purpose).

pub mod codec;
pub mod coding;
pub mod container;
pub mod error;

pub use codec::{
    decode_stream, encode_stream as encode_container, CodecReport, Limits, LossPattern,
};
pub use coding::{encode_stripe, recover_stripe};
pub use container::Header;
pub use error::{Error, Result};
