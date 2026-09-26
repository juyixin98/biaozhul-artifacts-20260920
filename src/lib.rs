//! ecstripe — erasure-coded stripe recovery.
//!
//! A pure-backend streaming codec that splits an input byte stream into
//! stripes, encodes each stripe with a systematic Reed-Solomon code over
//! GF(2^8) (k data shards + m parity shards), and recovers the stream when
//! up to m shards per stripe are known to be missing.
//!
//! * Binary container format: documented in [`format`].
//! * Field arithmetic: the mature [`gf256`] crate (polynomial 0x11d).
//! * Matrix construction and erasure recovery: implemented in this crate
//!   ([`gfmat`], [`stripe`]).
//! * JSON control entry: [`control`].
//!
//! Guarantees and limits:
//! * Only KNOWN erasures are recovered, and only up to `m` of them per
//!   stripe. Silent corruption is NOT detected by the format — verify
//!   integrity externally (the decode request accepts `expected_sha256`).
//! * Memory is bounded to one stripe: (k+m) * stripe_size bytes, further
//!   capped by `max_stripe_memory`.
//! * Decoded output length is capped by `max_output_bytes`.

pub mod codec;
pub mod control;
pub mod error;
pub mod format;
pub mod gfmat;
pub mod stripe;

pub use error::{Error, Result};
