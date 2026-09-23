//! FIPS 180-4 SHA-1 纯实现（仅用于 WebSocket 握手 `Sec-WebSocket-Accept`）。
//!
//! 不依赖任何外部 crate。SHA-1 在这里只作为握手握手串的指纹算法（RFC 6455 §4.2.2
//! 规定），不用于任何安全敏感的新场景。

/// SHA-1 初始哈希值（FIPS 180-4 §5.3.1）。
const H0: [u32; 5] = [
    0x6745_2301,
    0xEFCD_AB89,
    0x98BA_DCFE,
    0x1032_5476,
    0xC3D2_E1F0,
];

/// 增量/一次性均可的 SHA-1 计算器。
#[derive(Clone)]
pub struct Sha1 {
    h: [u32; 5],
    buf: [u8; 64],
    buf_len: usize,
    total_len: u64,
}

impl Default for Sha1 {
    fn default() -> Self {
        Self::new()
    }
}

impl Sha1 {
    pub fn new() -> Self {
        Self {
            h: H0,
            buf: [0u8; 64],
            buf_len: 0,
            total_len: 0,
        }
    }

    pub fn update(&mut self, mut data: &[u8]) {
        self.total_len = self.total_len.wrapping_add(data.len() as u64);

        if self.buf_len > 0 {
            let need = 64 - self.buf_len;
            let take = need.min(data.len());
            self.buf[self.buf_len..self.buf_len + take].copy_from_slice(&data[..take]);
            self.buf_len += take;
            data = &data[take..];
            if self.buf_len == 64 {
                let block = self.buf;
                self.process_block(&block);
                self.buf_len = 0;
            }
        }

        while data.len() >= 64 {
            let mut block = [0u8; 64];
            block.copy_from_slice(&data[..64]);
            self.process_block(&block);
            data = &data[64..];
        }

        if !data.is_empty() {
            self.buf[..data.len()].copy_from_slice(data);
            self.buf_len = data.len();
        }
    }

    pub fn finalize(mut self) -> [u8; 20] {
        // 追加 0x80，再补 0，最后附 64 位大端位长度（FIPS 180-4 §5.1.1）。
        let bit_len = self.total_len.wrapping_mul(8);
        self.buf[self.buf_len] = 0x80;
        for b in &mut self.buf[self.buf_len + 1..] {
            *b = 0;
        }

        if self.buf_len + 1 > 56 {
            // 当前块放不下 8 字节长度：先处理当前块，再开一块全零 + 长度。
            let block = self.buf;
            self.process_block(&block);
            self.buf = [0u8; 64];
        }
        self.buf[56..64].copy_from_slice(&bit_len.to_be_bytes());
        let block = self.buf;
        self.process_block(&block);

        let mut out = [0u8; 20];
        for (i, word) in self.h.iter().enumerate() {
            out[i * 4..i * 4 + 4].copy_from_slice(&word.to_be_bytes());
        }
        out
    }

    fn process_block(&mut self, block: &[u8; 64]) {
        let mut w = [0u32; 80];
        for t in 0..16 {
            w[t] = u32::from_be_bytes([
                block[t * 4],
                block[t * 4 + 1],
                block[t * 4 + 2],
                block[t * 4 + 3],
            ]);
        }
        for t in 16..80 {
            w[t] = w[t - 3] ^ w[t - 8] ^ w[t - 14] ^ w[t - 16];
            w[t] = w[t].rotate_left(1);
        }

        let mut a = self.h[0];
        let mut b = self.h[1];
        let mut c = self.h[2];
        let mut d = self.h[3];
        let mut e = self.h[4];

        for (t, w_t) in w.iter().enumerate() {
            let (k, f) = match t {
                0..=19 => (0x5A82_7999, (b & c) | ((!b) & d)),
                20..=39 => (0x6ED9_EBA1, b ^ c ^ d),
                40..=59 => (0x8F1B_BCDC, (b & c) | (b & d) | (c & d)),
                _ => (0xCA62_C1D6, b ^ c ^ d),
            };
            let temp = a
                .rotate_left(5)
                .wrapping_add(f)
                .wrapping_add(e)
                .wrapping_add(k)
                .wrapping_add(*w_t);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = temp;
        }

        self.h[0] = self.h[0].wrapping_add(a);
        self.h[1] = self.h[1].wrapping_add(b);
        self.h[2] = self.h[2].wrapping_add(c);
        self.h[3] = self.h[3].wrapping_add(d);
        self.h[4] = self.h[4].wrapping_add(e);
    }
}

/// 便捷函数：一次计算 SHA-1。
pub fn sha1(data: &[u8]) -> [u8; 20] {
    let mut s = Sha1::new();
    s.update(data);
    s.finalize()
}

/// RFC 4648 标准 Base64 编码（握手响应需要，自行实现避免依赖）。
pub fn base64_encode(data: &[u8]) -> String {
    const TABLE: &[u8; 64] =
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let triple = (b0 << 16) | (b1 << 8) | b2;
        out.push(TABLE[((triple >> 18) & 0x3F) as usize] as char);
        out.push(TABLE[((triple >> 12) & 0x3F) as usize] as char);
        if chunk.len() > 1 {
            out.push(TABLE[((triple >> 6) & 0x3F) as usize] as char);
        } else {
            out.push('=');
        }
        if chunk.len() > 2 {
            out.push(TABLE[(triple & 0x3F) as usize] as char);
        } else {
            out.push('=');
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hex(bytes: &[u8]) -> String {
        bytes.iter().map(|b| format!("{b:02x}")).collect()
    }

    #[test]
    fn known_sha1_vectors() {
        // FIPS 180-4 / NIST 标准测试向量
        assert_eq!(hex(&sha1(b"")), "da39a3ee5e6b4b0d3255bfef95601890afd80709");
        assert_eq!(hex(&sha1(b"abc")), "a9993e364706816aba3e25717850c26c9cd0d89d");
        // 跨 64 字节块边界的长度（55 与 56 字节覆盖两种 padding 分支）
        assert_eq!(
            hex(&sha1(&[b'a'; 55][..])),
            "c1c8bbdc22796e28c0e15163d20899b65621d65a"
        );
        assert_eq!(
            hex(&sha1(&[b'a'; 56][..])),
            "c2db330f6083854c99d4b5bfb6e8f29f201be699"
        );
        assert_eq!(
            hex(&sha1(&[b'a'; 64][..])),
            "0098ba824b5c16427bd7a1122a5a442a25ec644d"
        );
        assert_eq!(
            hex(&sha1(b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq")),
            "84983e441c3bd26ebaae4aa1f95129e5e54670f1"
        );
    }

    #[test]
    fn incremental_matches_one_shot() {
        let input = b"the quick brown fox jumps over the lazy dog";
        let one = sha1(input);
        let mut s = Sha1::new();
        for &b in input {
            s.update(&[b]);
        }
        assert_eq!(s.finalize(), one);
    }

    #[test]
    fn base64_known_vectors() {
        assert_eq!(base64_encode(b""), "");
        assert_eq!(base64_encode(b"f"), "Zg==");
        assert_eq!(base64_encode(b"fo"), "Zm8=");
        assert_eq!(base64_encode(b"foo"), "Zm9v");
        assert_eq!(base64_encode(b"foob"), "Zm9vYg==");
        // RFC 6455 §4.2.2 的握手示例：
        // key "dGhlIHNhbXBsZSBub25jZQ==" + GUID 的 SHA-1 应为 s3pPLMBiTxaQ9kYGzzhZRbK+xOo=
        let mut input = b"dGhlIHNhbXBsZSBub25jZQ==".to_vec();
        input.extend_from_slice(b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11");
        assert_eq!(
            base64_encode(&sha1(&input)),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
        );
    }
}
