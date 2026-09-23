//! Streaming line reader.
//!
//! Unlike `std::io::BufRead::read_line`, this reader never rejects a line for
//! being "too long": the working buffer grows without a fixed cap so that an
//! arbitrarily large single record is handled correctly. The sort's memory
//! *budget* only governs how many records are held before a segment is
//! spilled; an oversized record produces a one-record run on its own.
//!
//! Both `\n` and `\r\n` are accepted as line terminators; a final line not
//! terminated by a newline is still delivered. The terminator bytes are never
//! included in the returned record.

use crate::error::Result;
use crate::fs::{BufReader, RFile};

pub struct LineReader {
    inner: BufReader<Box<dyn RFile>>,
    buf: Vec<u8>,
    pos: usize,
    len: usize,
    eof: bool,
    bytes_read: u64,
}

impl LineReader {
    pub fn new(file: Box<dyn RFile>, io_buf: usize) -> Self {
        LineReader {
            inner: BufReader::new(file, io_buf),
            buf: vec![0u8; io_buf.max(64)],
            pos: 0,
            len: 0,
            eof: false,
            bytes_read: 0,
        }
    }

    pub fn bytes_read(&self) -> u64 {
        self.bytes_read
    }

    fn pull(&mut self) -> Result<bool> {
        if self.eof {
            return Ok(false);
        }
        if self.pos > 0 {
            self.buf.copy_within(self.pos..self.len, 0);
            self.len -= self.pos;
            self.pos = 0;
        }
        if self.len == self.buf.len() {
            // A single line spans more than the whole buffer: grow.
            let new_len = self.buf.len().saturating_mul(2);
            self.buf.resize(new_len, 0);
        }
        let n = self.inner.read(&mut self.buf[self.len..])?;
        self.bytes_read = self.bytes_read.saturating_add(n as u64);
        if n == 0 {
            self.eof = true;
        } else {
            self.len += n;
        }
        Ok(n > 0)
    }

    /// Returns the next line without terminator, or `None` at EOF.
    pub fn read_line(&mut self) -> Result<Option<Vec<u8>>> {
        loop {
            if self.pos < self.len {
                if let Some(rel) = self.buf[self.pos..self.len].iter().position(|&b| b == b'\n') {
                    let mut end = self.pos + rel;
                    let start = self.pos;
                    self.pos = end + 1;
                    if end > start && self.buf[end - 1] == b'\r' {
                        end -= 1;
                    }
                    return Ok(Some(self.buf[start..end].to_vec()));
                }
            } else if self.eof {
                return Ok(None);
            }
            let had_data = self.pos < self.len;
            if !self.pull()? {
                if had_data {
                    let mut end = self.len;
                    let start = self.pos;
                    self.pos = self.len;
                    if end > start && self.buf[end - 1] == b'\r' {
                        end -= 1;
                    }
                    return Ok(Some(self.buf[start..end].to_vec()));
                }
                return Ok(None);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fs;

    struct SliceReader(Vec<u8>, usize);
    impl RFile for SliceReader {
        fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
            let n = buf.len().min(self.0.len().saturating_sub(self.1));
            buf[..n].copy_from_slice(&self.0[self.1..self.1 + n]);
            self.1 += n;
            Ok(n)
        }
    }

    fn lines(data: &str, io_buf: usize) -> Vec<String> {
        let r = Box::new(SliceReader(data.as_bytes().to_vec(), 0));
        let mut lr = LineReader::new(r, io_buf);
        let mut out = Vec::new();
        while let Some(l) = lr.read_line().unwrap() {
            out.push(String::from_utf8(l).unwrap());
        }
        out
    }

    #[test]
    fn basics_and_crlf() {
        assert_eq!(lines("a\nbb\nccc\n", 4), vec!["a", "bb", "ccc"]);
        assert_eq!(lines("a\r\nb\r\n", 4), vec!["a", "b"]);
        assert_eq!(lines("only", 4), vec!["only"]);
        assert_eq!(lines("only\r\n", 4), vec!["only"]);
        assert_eq!(lines("", 4).len(), 0);
    }

    #[test]
    fn oversized_line_grows_buffer() {
        let big = "x".repeat(1000);
        let data = format!("small\n{big}\nend");
        let got = lines(&data, 8);
        assert_eq!(got, vec!["small".to_owned(), big, "end".to_owned()]);
    }

    #[test]
    fn empty_lines_preserved() {
        assert_eq!(lines("\n\nx\n", 16), vec!["", "", "x"]);
    }

    // keeps `fs` import used in case of later extension; silence warning now
    #[allow(dead_code)]
    fn _use_fs(_: &dyn fs::Fs) {}
}
