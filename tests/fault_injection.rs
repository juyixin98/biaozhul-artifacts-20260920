//! 故障注入测试：通过可注入 I/O 层模拟写失败、sync 失败、撕裂写、读失败。

use sst::{Error, FaultyWriter, MemStorage, ReadAt, Result, TableOptions, TableReader, TableWriter};

fn write_table<W: sst::WriteSink>(sink: W) -> Result<()> {
    let mut w = TableWriter::new(
        sink,
        TableOptions {
            block_size: 128,
            restart_interval: 2,
        },
    );
    for i in 0..100 {
        w.add(format!("key{:04}", i).as_bytes(), b"v")?;
    }
    w.finish()?;
    Ok(())
}

#[test]
fn sync_failure_aborts_finish() {
    let mem = MemStorage::new();
    let mut fw = FaultyWriter::new(mem);
    fw.fail_sync = true;
    let err = write_table(fw).expect_err("sync failure must abort finish");
    assert!(matches!(err, Error::Io(_)), "unexpected: {err}");
}

#[test]
fn write_failure_propagates() {
    // 在每一次 write_all 调用点注入失败，都必须以 Err 传播
    for fail_at in 0..6 {
        let mem = MemStorage::new();
        let mut fw = FaultyWriter::new(mem);
        fw.fail_on_call = Some(fail_at);
        // 调用次数取决于块数；只要注入点落在实际调用内就必须失败。
        // 为确定哪些调用点有效，先统计正常构建的写调用数。
        let probe = MemStorage::new();
        let calls = {
            let mut counting = CountingWriter::new(probe);
            write_table(&mut counting).unwrap();
            counting.calls
        };
        let result = write_table(fw);
        if fail_at < calls {
            assert!(result.is_err(), "fail_at={fail_at} should error");
        } else {
            assert!(result.is_ok(), "fail_at={fail_at} beyond write count");
        }
    }
}

/// 统计 write_all 调用次数的包装器（测试辅助）。
struct CountingWriter<W: sst::WriteSink> {
    inner: W,
    calls: usize,
}
impl<W: sst::WriteSink> CountingWriter<W> {
    fn new(inner: W) -> Self {
        CountingWriter { inner, calls: 0 }
    }
}
impl<W: sst::WriteSink> sst::WriteSink for &mut CountingWriter<W> {
    fn write_all(&mut self, buf: &[u8]) -> Result<()> {
        self.calls += 1;
        self.inner.write_all(buf)
    }
    fn sync(&mut self) -> Result<()> {
        self.inner.sync()
    }
}

#[test]
fn torn_write_is_detected_on_open_or_verify() {
    // 统计写调用次数，定位“写页脚”那一次（最后一次）
    let probe = MemStorage::new();
    let calls = {
        let mut counting = CountingWriter::new(probe);
        write_table(&mut counting).unwrap();
        counting.calls
    };
    let footer_call = calls - 1;

    // 撕裂写：页脚 32 字节只落 16 字节，但向上报告成功
    let mem = MemStorage::new();
    let mut fw = FaultyWriter::new(mem.clone());
    fw.partial_on_call = Some((footer_call, 16));
    write_table(fw).expect("torn write is invisible to the writer");

    // 读者必须发现文件不完整（页脚被截断 → 魔数不对）
    let err = match TableReader::open(mem.clone()) {
        Err(e) => e,
        Ok(t) => {
            // 即便 open 侥幸通过，verify 也必须抓住
            t.verify().expect_err("verify must catch torn footer");
            return;
        }
    };
    assert!(err.to_string().contains("corruption"), "unexpected: {err}");
}

#[test]
fn torn_data_block_detected_by_crc() {
    // 第一个数据块只写一半：后续字节全部前移，文件不再符合格式
    let mem = MemStorage::new();
    let mut fw = FaultyWriter::new(mem.clone());
    fw.partial_on_call = Some((0, 10)); // 第 0 次写 = 第一个数据块
    write_table(fw).unwrap();

    // open 或 verify 必须报错（索引边界对不上 / CRC 不匹配）
    match TableReader::open(mem) {
        Err(e) => assert!(e.to_string().contains("corruption"), "unexpected: {e}"),
        Ok(t) => {
            t.verify().expect_err("verify must catch torn data block");
        }
    }
}

/// 读取侧故障注入：所有 read_at 都失败。
struct FailingReader;

impl ReadAt for FailingReader {
    fn read_at(&self, _buf: &mut [u8], _offset: u64) -> Result<()> {
        Err(Error::Io(std::io::Error::other(
            "injected read failure",
        )))
    }
    fn len(&self) -> Result<u64> {
        Ok(10_000) // 长度正常，读全挂
    }
}

#[test]
fn read_failure_surfaces_as_error() {
    let err = TableReader::open(FailingReader).expect_err("read failure must abort open");
    assert!(matches!(err, Error::Io(_)), "unexpected: {err}");
}
