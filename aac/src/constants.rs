//! Fixed constants of the integer-range arithmetic coder.
//!
//! All values here are part of the documented wire format (see `FORMAT.md`).
//! Changing any of them changes the bitstream, so they are never configurable.

/// Number of bits in the integer coding range (`low`, `high` are kept in
/// `[0, 2^CODE_BITS)`).
pub const CODE_BITS: u32 = 32;
/// `2^CODE_BITS`, as a 64-bit value.
pub const TOP: u64 = 1u64 << CODE_BITS;
/// Half of the range.
pub const HALF: u64 = TOP / 2;
/// Quarter of the range (midpoint of the lower half).
pub const QUARTER: u64 = TOP / 4;
/// Renormalization floor: while `high - low < QUARTER`, E3 scaling is applied.
pub const MIN_RANGE: u64 = QUARTER;

/// Maximum total model frequency allowed before rescaling.
///
/// Chosen so that `range * total <= 2^32 * 2^14 = 2^46`, safely inside `u64`.
pub const MAX_FREQUENCY: u32 = 1 << 14; // 16384

/// Initial frequency count of every symbol (including EOF).
pub const INITIAL_FREQUENCY: u32 = 1;
/// Frequency floor after rescaling: every symbol that still occurs keeps at
/// least this count.
pub const RESCALE_FLOOR: u32 = 1;

/// Number of byte symbols (0..=255).
pub const NUM_BYTES: usize = 256;
/// Symbol identifying the end-of-stream terminator.
pub const EOF_SYMBOL: u16 = 256;
/// Total alphabet size (256 bytes + EOF).
pub const NUM_SYMBOLS: usize = 257;

/// Default cap on decoded output length, when the caller supplies none.
pub const DEFAULT_MAX_OUTPUT: u64 = 1 << 30; // 1 GiB
