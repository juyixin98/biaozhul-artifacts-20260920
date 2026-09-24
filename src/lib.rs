//! Custom binary serial-frame protocol: real encoder, and an incremental,
//! memory-bounded parser that tolerates arbitrary byte slicing, glued/partial
//! frames, noise, bad CRCs, oversized length claims and `u16` sequence wrap.

pub mod codec;
pub mod crc;
pub mod http;
pub mod parser;
pub mod sample;

pub use codec::{
    encode_frame, EncodeError, Frame, DEFAULT_MAX_PAYLOAD, FRAME_OVERHEAD, HEADER_LEN, MAGIC,
    WIRE_MAX_PAYLOAD,
};
pub use parser::{Event, FrameParser, ParseStats};
