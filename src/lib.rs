//! Page-level copy-on-write branching snapshot store.
//!
//! See [`store::Store`] for the engine and `src/main.rs` for the Axum server.

pub mod base64;
pub mod server;
pub mod store;

pub use store::Store;
