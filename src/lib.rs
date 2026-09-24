//! repro-pack: deterministic, reproducible artifact packaging.
//!
//! See `pack` for the packaging rules and `server` for the HTTP layer.

pub mod pack;
pub mod server;
pub mod verify;

pub use pack::{
    collect_entries, pack, CollectedEntry, EntryKind, Error, Manifest, ManifestEntry, PackOptions,
    PackOutcome,
};
pub use verify::{verify, VerifyOptions, VerifyOutcome};
