//! Unit tests for the low-level codec primitives.

use rbitmap::codec::*;
use rbitmap::error::RbError;

#[test]
fn varint_roundtrip_boundaries() {
    let cases: Vec<u64> = vec![
        0,
        1,
        127,
        128,
        300,
        16383,
        16384,
        u32::MAX as u64,
        u32::MAX as u64 + 1,
        u64::MAX,
    ];
    for v in cases {
        let mut buf = Vec::new();
        write_varint(&mut buf, v);
        let mut pos = 0;
        let got = read_varint(&buf, &mut pos).expect("decode");
        assert_eq!(got, v, "roundtrip {v}");
        assert_eq!(pos, buf.len());
    }
}

#[test]
fn varint_rejects_overlong_and_noncanonical() {
    // 11 continuation bytes.
    let too_long = [0xffu8; 11];
    let mut pos = 0;
    assert!(read_varint(&too_long, &mut pos).is_err());

    // Non-canonical: 0x80 0x00 encodes 0 in two bytes.
    let noncanon = [0x80u8, 0x00];
    let mut pos = 0;
    assert!(read_varint(&noncanon, &mut pos).is_err());

    // Truncated.
    let trunc = [0x80u8];
    let mut pos = 0;
    assert!(matches!(
        read_varint(&trunc, &mut pos),
        Err(RbError::UnexpectedEof { .. })
    ));
}

#[test]
fn writer_reader_roundtrip() {
    let mut w = Writer::new();
    w.u8(0xAB);
    w.u16(0xBEEF);
    w.u32(0xDEADBEEF);
    w.u64(0x1122334455667788);
    w.bytes(b"hi");
    w.len_varint(65536);
    let buf = w.into_bytes();

    let mut r = Reader::new(&buf);
    assert_eq!(r.u8().unwrap(), 0xAB);
    assert_eq!(r.u16().unwrap(), 0xBEEF);
    assert_eq!(r.u32().unwrap(), 0xDEADBEEF);
    assert_eq!(r.u64().unwrap(), 0x1122334455667788);
    assert_eq!(r.take_n(2, "x").unwrap(), b"hi");
    assert_eq!(r.bounded_varint(1 << 20, "len").unwrap(), 65536);
    assert_eq!(r.remaining(), 0);
}

#[test]
fn bounded_varint_enforces_limit_before_read() {
    let mut buf = Vec::new();
    write_varint(&mut buf, 1_000_000);
    let mut r = Reader::new(&buf);
    let err = r.bounded_varint(10, "count").unwrap_err();
    assert!(matches!(
        err,
        RbError::LengthExceeded {
            declared: 1_000_000,
            limit: 10,
            ..
        }
    ));
}

#[test]
fn io_stream_roundtrip() {
    let mut sink: Vec<u8> = Vec::new();
    {
        let mut enc = IoEncoder::new(&mut sink);
        enc.u8(7).unwrap();
        enc.u16(65535).unwrap();
        enc.u32(4294967295).unwrap();
        enc.len_varint(300).unwrap();
        enc.bytes(b"payload").unwrap();
        enc.flush().unwrap();
    }
    let mut dec = IoDecoder::new(&sink[..]);
    assert_eq!(dec.u8().unwrap(), 7);
    assert_eq!(dec.u16().unwrap(), 65535);
    assert_eq!(dec.u32().unwrap(), 4294967295);
    assert_eq!(dec.bounded_varint(1000, "len").unwrap(), 300);
    let mut p = [0u8; 7];
    dec.take_into(&mut p, "payload").unwrap();
    assert_eq!(&p, b"payload");
}

#[test]
fn io_decoder_eof_is_error_not_panic() {
    let data = [1u8, 2];
    let mut dec = IoDecoder::new(&data[..]);
    assert!(dec.u32().is_err());
}
