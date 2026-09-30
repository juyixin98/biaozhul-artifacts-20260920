//! # tritmap
//!
//! Column-value bitmap indexes with SQL three-valued-logic filtering
//! (`AND`/`OR`/`NOT`/`IS NULL`) over a versioned, deletion-aware document
//! universe.
//!
//! Layers (see module docs for details):
//!
//! | Layer | Modules |
//! |-------|---------|
//! | Typed batches | [`batch`] (arrow2) |
//! | Bitmap core | [`bitset`], [`trit`], [`partition`] |
//! | Query operators | [`index`], [`query`], [`deletion`] |
//! | Resources & state | [`dataset`], [`server::state`] |
//! | Verification | [`oracle`], [`verify`] |
//! | Config / IO | [`config`], [`loader`], [`server`] |

pub mod batch;
pub mod bitset;
pub mod config;
pub mod dataset;
pub mod deletion;
pub mod error;
pub mod index;
pub mod loader;
pub mod oracle;
pub mod partition;
pub mod query;
pub mod server;
pub mod trit;
pub mod verify;

pub use error::{Result, TritmapError};
