//! TCP 传输帧（RFC 1035 §4.2.2）：两字节网络序长度前缀 + 报文。
//!
//! 接收是**增量**的：先读满 2 字节前缀，再按声明长度读满报文体；
//! 对端提前关闭时区分“干净 EOF”（未开始读）与“截断”（读到一半）。

use std::io::{self, Read, Write};

/// TCP 帧相关错误。
#[derive(Debug)]
pub enum FrameError {
    /// 底层 I/O 错误。
    Io(io::Error),
    /// 声明长度超过本端允许的最大报文长度。
    MessageTooLong {
        /// 对端声明的长度
        declared: usize,
        /// 本端上限
        max: usize,
    },
    /// 读到一半连接被关闭（截断的帧）。
    UnexpectedEof,
    /// 未开始读任何字节时连接已关闭（干净结束，不算错误场景但需区分）。
    Closed,
}

impl std::fmt::Display for FrameError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            FrameError::Io(e) => write!(f, "I/O 错误：{e}"),
            FrameError::MessageTooLong { declared, max } => {
                write!(f, "声明的报文长度 {declared} 超过上限 {max}")
            }
            FrameError::UnexpectedEof => write!(f, "帧读到一半连接被关闭"),
            FrameError::Closed => write!(f, "连接已关闭"),
        }
    }
}

impl std::error::Error for FrameError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            FrameError::Io(e) => Some(e),
            _ => None,
        }
    }
}

impl From<io::Error> for FrameError {
    fn from(e: io::Error) -> Self {
        FrameError::Io(e)
    }
}

/// 读满 `buf`，返回实际读到的字节数（EOF 时可能少于 buf 长度）。
fn read_full(r: &mut impl Read, buf: &mut [u8]) -> io::Result<usize> {
    let mut n = 0usize;
    while n < buf.len() {
        match r.read(&mut buf[n..]) {
            Ok(0) => break,
            Ok(m) => n += m,
            Err(ref e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        }
    }
    Ok(n)
}

/// 接收一帧：先 2 字节长度，再报文体。`max` 为本端允许的最大报文长度。
pub fn recv_frame(r: &mut impl Read, max: usize) -> Result<Vec<u8>, FrameError> {
    let mut hdr = [0u8; 2];
    let n = read_full(r, &mut hdr)?;
    if n == 0 {
        return Err(FrameError::Closed);
    }
    if n < 2 {
        return Err(FrameError::UnexpectedEof);
    }
    let len = u16::from_be_bytes(hdr) as usize;
    if len > max {
        return Err(FrameError::MessageTooLong { declared: len, max });
    }
    let mut body = vec![0u8; len];
    let got = read_full(r, &mut body)?;
    if got < len {
        return Err(FrameError::UnexpectedEof);
    }
    Ok(body)
}

/// 发送一帧：自动加两字节长度前缀。
pub fn send_frame(w: &mut impl Write, msg: &[u8]) -> Result<(), FrameError> {
    if msg.len() > u16::MAX as usize {
        return Err(FrameError::MessageTooLong {
            declared: msg.len(),
            max: u16::MAX as usize,
        });
    }
    w.write_all(&(msg.len() as u16).to_be_bytes())?;
    w.write_all(msg)?;
    w.flush()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn frame_roundtrip() {
        let mut buf: Vec<u8> = Vec::new();
        send_frame(&mut buf, b"hello").unwrap();
        send_frame(&mut buf, b"").unwrap();
        let mut cur = Cursor::new(buf);
        assert_eq!(recv_frame(&mut cur, 1024).unwrap(), b"hello");
        assert_eq!(recv_frame(&mut cur, 1024).unwrap(), b"");
        assert!(matches!(
            recv_frame(&mut cur, 1024),
            Err(FrameError::Closed)
        ));
    }

    #[test]
    fn frame_too_long() {
        let data = [0u8, 10, 1, 2, 3]; // 声明 10 字节
        let mut cur = Cursor::new(data.to_vec());
        assert!(matches!(
            recv_frame(&mut cur, 4),
            Err(FrameError::MessageTooLong {
                declared: 10,
                max: 4
            })
        ));
    }

    #[test]
    fn frame_truncated() {
        let data = [0u8, 5, 1, 2]; // 声明 5 字节，只给了 2 字节
        let mut cur = Cursor::new(data.to_vec());
        assert!(matches!(
            recv_frame(&mut cur, 1024),
            Err(FrameError::UnexpectedEof)
        ));
    }
}
