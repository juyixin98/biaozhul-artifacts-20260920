//! Injectable I/O layer: injected write/read faults surface as `Error::Io`,
//! the storage layer retries partial writes and fragmented reads, and the
//! durability boundary (`sync`) is exercised explicitly.

mod common;

use common::*;
use psst::error::Error;
use psst::io::{
    append_all, read_fill, FaultyReader, FaultyWriter, MemReader, MemStore, MemWriter, ReadFaults,
    WriteFaults,
};
use psst::table::{collect_scan, Options, ScanOptions, Table, TableBuilder};

fn sample_pairs() -> Vec<(Vec<u8>, Vec<u8>)> {
    (0..300u32)
        .map(|i| {
            (
                format!("k/{i:04}").into_bytes(),
                format!("v{i}").into_bytes(),
            )
        })
        .collect()
}

#[test]
fn injected_append_failure_aborts_build_and_leaves_no_success() {
    let pairs = sample_pairs();
    let store = MemStore::new();
    let writer = FaultyWriter::new(
        MemWriter::new(store.clone()),
        WriteFaults {
            // The very first data block append fails.
            fail_append_at: Some(0),
            ..WriteFaults::default()
        },
    );
    let mut builder = TableBuilder::new(writer, Options::default()).unwrap();
    let mut failed = false;
    for (k, v) in &pairs {
        if builder.add(k, v).is_err() {
            failed = true;
            break;
        }
    }
    if !failed {
        let err = builder.finish().unwrap_err();
        assert!(matches!(err, Error::Io(_)), "got {err}");
    }
    // Whatever partial bytes exist must not parse as a table.
    let open = Table::open(MemReader::new(store.clone()), store.len() as u64);
    assert!(matches!(
        open,
        Err(Error::Io(_)) | Err(Error::Corruption(_))
    ));
}

#[test]
fn injected_late_append_failure_aborts_build() {
    // Tiny blocks produce many append calls; a high call index lands deep in
    // the build (possibly in finish), and wherever it lands it must surface as
    // an IO error rather than a corrupt-but-accepted file.
    let pairs = sample_pairs();
    let store = MemStore::new();
    let writer = FaultyWriter::new(
        MemWriter::new(store.clone()),
        WriteFaults {
            fail_append_at: Some(40),
            ..WriteFaults::default()
        },
    );
    let mut builder = TableBuilder::new(
        writer,
        Options {
            block_size: 64,
            restart_interval: 4,
        },
    )
    .unwrap();

    let mut failed = false;
    for (k, v) in &pairs {
        if builder.add(k, v).is_err() {
            failed = true;
            break;
        }
    }
    if !failed {
        let err = builder.finish().unwrap_err();
        assert!(matches!(err, Error::Io(_)), "finish error: {err}");
    }

    let open = Table::open(MemReader::new(store.clone()), store.len() as u64);
    assert!(matches!(
        open,
        Err(Error::Io(_)) | Err(Error::Corruption(_))
    ));
}

#[test]
fn injected_sync_failure_propagates_from_finish() {
    let pairs = sample_pairs();
    let store = MemStore::new();
    let writer = FaultyWriter::new(
        MemWriter::new(store),
        WriteFaults {
            fail_sync_at: Some(0),
            ..WriteFaults::default()
        },
    );
    let mut builder = TableBuilder::new(writer, Options::default()).unwrap();
    for (k, v) in &pairs {
        builder.add(k, v).unwrap();
    }
    let err = builder.finish().unwrap_err();
    assert!(matches!(err, Error::Io(_)), "sync error: {err}");
}

#[test]
fn short_writes_are_retried_and_file_is_byte_identical() {
    let pairs = sample_pairs();
    let (good_store, size) = build_mem(&pairs, tiny_options()).unwrap();

    // Every append accepts at most 3 bytes; the builder's retry loop must
    // produce a byte-identical file.
    let faulty_store = MemStore::new();
    let writer = AlwaysShort {
        inner: MemWriter::new(faulty_store.clone()),
        max: 3,
    };
    let mut builder = TableBuilder::new(writer, tiny_options()).unwrap();
    for (k, v) in &pairs {
        builder.add(k, v).unwrap();
    }
    let stats = builder.finish().unwrap();
    assert_eq!(stats.bytes, size);
    assert_eq!(faulty_store.snapshot(), good_store.snapshot());

    // And the resulting table reads correctly.
    let table = Table::open(MemReader::new(faulty_store.clone()), size).unwrap();
    table.validate().unwrap();
    let all = collect_scan(&table, ScanOptions::default()).unwrap();
    assert_eq!(all.len(), pairs.len());
}

#[test]
fn fragmented_reads_still_produce_correct_results() {
    let pairs = sample_pairs();
    let (store, size) = build_mem(&pairs, tiny_options()).unwrap();
    let reader = FaultyReader::new(
        MemReader::new(store),
        ReadFaults {
            max_bytes_per_read: Some(7),
            ..ReadFaults::default()
        },
    );
    let table = Table::open(reader, size).unwrap();
    table.validate().unwrap();
    for (k, v) in pairs.iter().step_by(7) {
        assert_eq!(table.get(k).unwrap().as_ref(), Some(v));
    }
    let all = collect_scan(&table, ScanOptions::default()).unwrap();
    assert_eq!(all.len(), pairs.len());
}

#[test]
fn injected_read_error_during_footer_open_surfaces_as_io() {
    let pairs = sample_pairs();
    let (store, size) = build_mem(&pairs, tiny_options()).unwrap();
    let reader = FaultyReader::new(
        MemReader::new(store),
        ReadFaults {
            // The first read fetches the footer; fail it.
            fail_read_at_call: Some(0),
            ..ReadFaults::default()
        },
    );
    let err = match Table::open(reader, size) {
        Err(e) => e,
        Ok(_) => panic!("expected injected IO error at open"),
    };
    assert!(matches!(err, Error::Io(_)), "got {err}");
}

#[test]
fn injected_read_error_during_get_surfaces_as_io() {
    let pairs = sample_pairs();
    let (store, size) = build_mem(&pairs, tiny_options()).unwrap();
    let reader = FaultyReader::new(
        MemReader::new(store),
        ReadFaults {
            // open() reads footer (1 call) + index block; fail call 2.
            fail_read_at_call: Some(2),
            ..ReadFaults::default()
        },
    );
    // Depending on fragmentation counts the failure may land in open or get;
    // either way it must be an IO error, never a wrong answer.
    match Table::open(reader, size) {
        Ok(table) => {
            let err = table.get(b"k/00150").unwrap_err();
            assert!(matches!(err, Error::Io(_)), "got {err}");
        }
        Err(Error::Io(_)) => {}
        Err(other) => panic!("expected IO error, got {other}"),
    }
}

#[test]
fn io_helpers_reject_impossible_counts() {
    let mut w = BadWriter;
    assert!(append_all(&mut w, b"abc").is_err());

    let mut buf = [0u8; 4];
    let r = OverReader;
    assert!(read_fill(&r, &mut buf, 0).is_err());
}

// ---------------------------------------------------------------------------
// Test-only writer wrappers
// ---------------------------------------------------------------------------

struct AlwaysShort<W> {
    inner: W,
    max: usize,
}
impl<W: psst::io::WritableFile> psst::io::WritableFile for AlwaysShort<W> {
    fn append(&mut self, data: &[u8]) -> psst::Result<usize> {
        let n = self.max.min(data.len()).max(1);
        self.inner.append(&data[..n])
    }
    fn sync(&mut self) -> psst::Result<()> {
        self.inner.sync()
    }
}

struct BadWriter;
impl psst::io::WritableFile for BadWriter {
    fn append(&mut self, _data: &[u8]) -> psst::Result<usize> {
        Ok(0) // append_all must treat zero progress as an error
    }
    fn sync(&mut self) -> psst::Result<()> {
        Ok(())
    }
}

struct OverReader;
impl psst::io::RandomAccessFile for OverReader {
    fn read_at(&self, buf: &mut [u8], _offset: u64) -> psst::Result<usize> {
        Ok(buf.len() + 1) // impossible count
    }
}
