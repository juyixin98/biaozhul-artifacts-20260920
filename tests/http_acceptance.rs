//! End-to-end acceptance checks through the real HTTP surface.
//!
//! The reference oracle is a plain uncompressed `Vec<(i64, i64)>` built
//! alongside the writes: every range response must equal the naive
//! `retain` filter over that array.

use std::path::PathBuf;

use tsblock::coding::Point;
use tsblock::server::{self, get, post, FaultMode};

fn tempdir(tag: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!(
        "tsblock-http-{tag}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&d).unwrap();
    d
}

/// Deterministic xorshift64* so the dataset is reproducible.
struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    fn range(&mut self, n: u64) -> i64 {
        (self.next() % n) as i64
    }
}

fn body_of(points: &[Point]) -> String {
    points
        .iter()
        .map(|p| format!("{},{}", p.ts, p.value))
        .collect::<Vec<_>>()
        .join("\n")
}

/// Parse `"points":[[i,i],[i,i]]` out of a response body.
fn parse_points(body: &str) -> Vec<Point> {
    let key = "\"points\":[";
    let start = body.find(key).unwrap() + key.len();
    // Empty array: `"points":[]` — no inner pairs at all.
    if body[start..].starts_with(']') {
        return Vec::new();
    }
    // The points array ends at the first `]]` (inner pair close + array close).
    let end = body[start..].find("]]").unwrap() + start;
    let inner = &body[start..end];
    inner
        .split("],[")
        .map(|pair| {
            let cleaned: String = pair
                .chars()
                .filter(|c| c.is_ascii_digit() || *c == ',' || *c == '-')
                .collect();
            let (ts, v) = cleaned.split_once(',').unwrap();
            Point::new(ts.parse().unwrap(), v.parse().unwrap())
        })
        .collect()
}

fn json_u64(body: &str, key: &str) -> u64 {
    let pat = format!("\"{key}\":");
    let i = body.find(&pat).unwrap() + pat.len();
    let rest = &body[i..];
    let end = rest
        .find(|c: char| !(c.is_ascii_digit() || c == '-' || c == '.'))
        .unwrap_or(rest.len());
    rest[..end].split('.').next().unwrap().parse().unwrap()
}

fn json_f64(body: &str, key: &str) -> f64 {
    let pat = format!("\"{key}\":");
    let i = body.find(&pat).unwrap() + pat.len();
    let rest = &body[i..];
    let end = rest
        .find(|c: char| !(c.is_ascii_digit() || c == '-' || c == '.'))
        .unwrap_or(rest.len());
    rest[..end].parse().unwrap()
}

fn reference_filter(points: &[Point], start: i64, end: i64) -> Vec<Point> {
    points
        .iter()
        .copied()
        .filter(|p| p.ts >= start && p.ts <= end)
        .collect()
}

#[test]
fn full_acceptance_over_http() {
    let dir = tempdir("accept");
    let handle = server::serve(
        "127.0.0.1:0",
        dir,
        100,
        FaultMode::On, // enables /dev/fault
    )
    .unwrap();
    let addr = handle.addr();

    // ---- build the reference dataset and write it ----------------------
    let mut rng = Rng(0x1234_5678_9ABC_DEF0);
    let n = 5000usize;
    let mut reference = Vec::with_capacity(n);
    let mut ts = 1_700_000_000i64;
    let mut value = -50i64;
    for i in 0..n {
        let delta = if i % 997 == 0 && i > 0 {
            0 // repeated timestamp every ~1000 points
        } else {
            58 + rng.range(7) // 58..=64: varied but non-decreasing
        };
        ts += delta;
        value += rng.range(7) - 3; // random walk crossing zero often
        reference.push(Point::new(ts, value));
    }

    let (st, body) = post(addr, "/series/ref?policy=reject&block_size=100", "");
    assert_eq!(st, 201, "{body}");

    // Write in several batches (none aligned to a block boundary).
    for chunk in reference.chunks(333) {
        let (st, body) = post(addr, "/series/ref/points", &body_of(chunk));
        assert_eq!(st, 200, "{body}");
    }
    let (st, body) = post(addr, "/series/ref/flush", "");
    assert_eq!(st, 200, "{body}");
    assert_eq!(json_u64(&body, "blocks_flushed"), 0); // already auto-flushed
    let (_, body) = get(addr, "/series/ref/stats");
    assert_eq!(json_u64(&body, "blocks"), 50); // 5000/100
    assert_eq!(json_u64(&body, "active_points"), 0);

    // ---- range queries vs the uncompressed oracle ----------------------
    let (st, body) = get(
        addr,
        &format!("/series/ref/range?start={}&end={}", i64::MIN, i64::MAX),
    );
    assert_eq!(st, 200, "{body}");
    assert_eq!(json_u64(&body, "count"), n as u64);
    assert_eq!(parse_points(&body), reference);

    // Boundary cases: exact endpoints, single point, empty window, ranges
    // spanning block boundaries, negatives in both dimensions.
    let windows = [
        (reference[0].ts, reference[0].ts),
        (reference[n - 1].ts, reference[n - 1].ts),
        (reference[1000].ts, reference[1234].ts),
        (reference[0].ts - 1, reference[0].ts),
        (reference[n - 1].ts, reference[n - 1].ts + 1),
        (reference[42].ts + 1, reference[43].ts - 1), // empty (strictly between)
        (-10_000_000_000, 10_000_000_000),
        (i64::MIN, i64::MAX),
    ];
    for (start, end) in windows {
        let (st, body) = get(addr, &format!("/series/ref/range?start={start}&end={end}"));
        assert_eq!(st, 200, "{body}");
        let got = parse_points(&body);
        let want = reference_filter(&reference, start, end);
        assert_eq!(got, want, "window [{start},{end}]");
    }

    // Duplicate timestamps: both points must be returned.
    let dup_ts = reference[997].ts;
    assert_eq!(reference[996].ts, dup_ts); // delta was 0 at i=997
    let dup_points = reference_filter(&reference, dup_ts, dup_ts);
    assert!(dup_points.len() >= 2);

    // ---- index pruning actually limits block reads ----------------------
    let start = reference[2500].ts;
    let end = reference[2503].ts;
    let (_, body) = get(addr, &format!("/series/ref/range?start={start}&end={end}"));
    let scanned = json_u64(&body, "blocks_scanned");
    assert!(scanned <= 2, "tight window scanned {scanned} blocks");

    // ---- compression ratio ---------------------------------------------
    let (_, body) = get(addr, "/series/ref/stats");
    let disk = json_u64(&body, "disk_bytes");
    let uncompressed = json_u64(&body, "uncompressed_bytes");
    assert_eq!(uncompressed, n as u64 * 16);
    let ratio = json_f64(&body, "compression_ratio");
    assert!(
        ratio > 2.0,
        "expected compressible walk, ratio={ratio} disk={disk}"
    );
    // Ratio must equal the honest arithmetic definition.
    let recomputed = uncompressed as f64 / disk as f64;
    assert!((ratio - recomputed).abs() < 1e-4);

    // ---- negatives and extreme integers --------------------------------
    let (st, _) = post(addr, "/series/extreme?policy=reject", "");
    assert_eq!(st, 201);
    let extreme = [
        Point::new(i64::MIN, i64::MIN),
        Point::new(i64::MIN, i64::MAX),
        Point::new(i64::MIN + 1, -1),
        Point::new(-1, i64::MIN),
        Point::new(0, 0),
        Point::new(i64::MAX - 1, i64::MAX),
        Point::new(i64::MAX, i64::MIN),
    ];
    let (st, body) = post(addr, "/series/extreme/points?sync=true", &body_of(&extreme));
    assert_eq!(st, 200, "{body}");
    let (_, body) = get(
        addr,
        &format!("/series/extreme/range?start={}&end={}", i64::MIN, i64::MAX),
    );
    assert_eq!(parse_points(&body), extreme);

    // ---- reject policy: whole batch refused, 409 ------------------------
    let (st, _) = post(addr, "/series/ord?policy=reject", "");
    assert_eq!(st, 201);
    let (st, _) = post(addr, "/series/ord/points", "10,1\n20,2");
    assert_eq!(st, 200);
    let (st, body) = post(addr, "/series/ord/points", "30,3\n5,4");
    assert_eq!(st, 409, "out-of-order batch must be rejected: {body}");
    let (_, body) = get(addr, "/series/ord/range?start=0&end=100");
    assert_eq!(
        parse_points(&body),
        vec![Point::new(10, 1), Point::new(20, 2)]
    );

    // ---- buffer policy: invisible until drain, then fully merged --------
    let (st, _) = post(addr, "/series/late?policy=buffer&block_size=2", "");
    assert_eq!(st, 201);
    let (st, _) = post(addr, "/series/late/points", "100,1\n200,2\n300,3\n400,4");
    assert_eq!(st, 200); // two flushed blocks already on disk
    let (st, body) = post(addr, "/series/late/points", "50,0\n250,9\n60,7");
    assert_eq!(st, 200, "{body}");
    assert_eq!(json_u64(&body, "buffered"), 3);
    let (_, body) = get(addr, "/series/late/buffered");
    assert_eq!(json_u64(&body, "buffered"), 3);
    let (_, body) = get(addr, "/series/late/range?start=0&end=1000");
    assert_eq!(
        parse_points(&body),
        vec![
            Point::new(100, 1),
            Point::new(200, 2),
            Point::new(300, 3),
            Point::new(400, 4)
        ]
    );
    let (st, body) = post(addr, "/series/late/drain", "");
    assert_eq!(st, 200, "{body}");
    assert_eq!(json_u64(&body, "merged"), 3);
    let (_, body) = get(addr, "/series/late/range?start=0&end=1000");
    assert_eq!(
        parse_points(&body),
        vec![
            Point::new(50, 0),
            Point::new(60, 7),
            Point::new(100, 1),
            Point::new(200, 2),
            Point::new(250, 9),
            Point::new(300, 3),
            Point::new(400, 4),
        ]
    );

    // ---- injected ENOSPC over HTTP is surfaced, retry succeeds ----------
    let (st, _) = post(addr, "/series/victim?policy=reject&block_size=10", "");
    assert_eq!(st, 201);
    let (st, body) = post(addr, "/dev/fault", "write_limit=30");
    assert_eq!(st, 200, "{body}");
    let pts: Vec<Point> = (0..10).map(|i| Point::new(1000 + i * 5, i)).collect();
    let (st, body) = post(addr, "/series/victim/points", &body_of(&pts));
    assert_eq!(st, 500, "ENOSPC must surface as 500: {body}");
    assert!(
        body.contains("os error 28") || body.contains("No space"),
        "{body}"
    );
    let (st, _) = post(addr, "/dev/fault", "clear");
    assert_eq!(st, 200);
    // The failed block is still in the active set; flush retries it.
    let (st, body) = post(addr, "/series/victim/flush", "");
    assert_eq!(st, 200, "{body}");
    assert_eq!(json_u64(&body, "blocks_flushed"), 1);

    handle.shutdown();
}

#[test]
fn bad_inputs_are_rejected_cleanly() {
    let dir = tempdir("bad");
    let handle = server::serve("127.0.0.1:0", dir, 100, FaultMode::Off).unwrap();
    let addr = handle.addr();

    let (st, _) = post(addr, "/series/ok?policy=reject", "");
    assert_eq!(st, 201);
    // non-numeric body
    let (st, _) = post(addr, "/series/ok/points", "not-a-point");
    assert_eq!(st, 400);
    // out-of-i64 integer
    let (st, _) = post(addr, "/series/ok/points", "9223372036854775808,1");
    assert_eq!(st, 400);
    // missing range params
    let (st, _) = get(addr, "/series/ok/range?start=1");
    assert_eq!(st, 400);
    // inverted range
    let (st, _) = get(addr, "/series/ok/range?start=10&end=1");
    assert_eq!(st, 400);
    // unknown series / route
    assert_eq!(get(addr, "/series/nope/range?start=0&end=1").0, 404);
    assert_eq!(get(addr, "/no/such/route").0, 404);
    // path traversal rejected
    let (st, _) = post(addr, "/series/..%2fetc?policy=reject", "");
    assert_eq!(st, 400);
    // fault endpoint disabled without --fault
    let (st, _) = post(addr, "/dev/fault", "clear");
    assert_eq!(st, 404);

    handle.shutdown();
}
