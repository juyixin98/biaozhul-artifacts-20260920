//! Library surface of the verifiable-computation host, used by both the
//! `vcr-host` binary and the integration tests.

pub mod io;
pub mod pipeline;
pub mod verify;

pub use io::{
    check_journal_binding, host_input_commitment, read_input, InputFile, PhaseTimings,
    VerifiedOutput,
};
pub use verify::{is_fake, receipt_kind_name, verify_strict};
