//! resp-incr: an incremental RESP2 byte parser.
//!
//! Supported subset (RESP2):
//!   - Simple strings  `+OK\r\n`
//!   - Errors          `-ERR message\r\n`
//!   - Integers        `:123\r\n`
//!   - Bulk strings    `$5\r\nhello\r\n` (binary-safe; `$-1\r\n` is null)
//!   - Arrays          `*2\r\n...`       (`*-1\r\n` is null)
//!
//! NOT supported (out of scope): RESP3 types (maps, sets, doubles, ...),
//! inline commands, streamed/ chunked replies.
//!
//! The parser is incremental: feed arbitrary byte fragments with
//! [`Parser::feed`], then drain complete messages with [`Parser::next`].
//! Limits (nesting depth, buffer budget, bulk/array length caps) are
//! configurable via [`Config`].

mod parser;
mod value;

pub use parser::{ParseError, Parser};
pub use value::{encode, Config, Value};
