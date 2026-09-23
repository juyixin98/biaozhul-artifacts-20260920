//! MQTT 会话子集测试服务入口。
//!
//! 用法：
//!   mqtt-subset-broker [--addr 127.0.0.1:1883] [--max-packet 262144]
//!                      [--retry-ms 2000] [--stats-interval 0] [--quiet]

use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use mqtt_subset::broker::{Broker, BrokerConfig};

static SHUTDOWN: AtomicBool = AtomicBool::new(false);

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut cfg = BrokerConfig::default();
    let mut stats_interval_secs: u64 = 0;

    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--addr" => {
                i += 1;
                cfg.bind_addr = args[i].clone();
            }
            "--max-packet" => {
                i += 1;
                cfg.max_packet_size = args[i].parse().expect("--max-packet needs integer");
            }
            "--retry-ms" => {
                i += 1;
                let ms: u64 = args[i].parse().expect("--retry-ms needs integer");
                cfg.retry_interval = Duration::from_millis(ms);
            }
            "--stats-interval" => {
                i += 1;
                stats_interval_secs = args[i].parse().expect("--stats-interval needs seconds");
            }
            "--quiet" => cfg.quiet = true,
            "-h" | "--help" => {
                println!(
                    "MQTT 3.1.1 session-subset test broker\n\
\n\
USAGE:\n    mqtt-subset-broker [OPTIONS]\n\
\n\
OPTIONS:\n\
    --addr <IP:PORT>          bind address (default 127.0.0.1:1883)\n\
    --max-packet <BYTES>      max remaining length per packet (default 262144)\n\
    --retry-ms <MILLIS>       QoS1 in-flight resend interval (default 2000)\n\
    --stats-interval <SECS>   print counters periodically (0 = on SIGTERM only)\n\
    --quiet                   suppress per-event logs\n\
    -h, --help                show this help"
                );
                return;
            }
            other => {
                eprintln!("unknown argument: {other} (try --help)");
                std::process::exit(2);
            }
        }
        i += 1;
    }

    let broker = Broker::start(cfg).expect("failed to bind broker");
    let addr = broker.local_addr().expect("local addr");
    eprintln!("[mqtt-subset] listening on {addr}");

    let stats = broker.stats();

    // 可选的周期性统计输出。
    let stats_thread = if stats_interval_secs > 0 {
        let stats = stats.clone();
        Some(std::thread::spawn(move || loop {
            std::thread::sleep(Duration::from_secs(stats_interval_secs));
            let line = format_counters(&stats);
            eprintln!("[mqtt-subset] stats: {line}");
        }))
    } else {
        None
    };

    // Ctrl-C：打印统计后停机。
    install_sigterm_handler();
    loop {
        std::thread::sleep(Duration::from_millis(200));
        if SHUTDOWN.load(Ordering::SeqCst) {
            break;
        }
    }
    eprintln!("[mqtt-subset] final stats: {}", format_counters(&stats));
    if let Some(t) = stats_thread {
        // 统计线程随进程退出即可。
        drop(t);
    }
    broker.shutdown();
    eprintln!("[mqtt-subset] stopped");
}

fn format_counters(stats: &mqtt_subset::broker::BrokerStats) -> String {
    let parts: Vec<String> = [
        (
            "connect_accepted",
            stats.connect_accepted.load(Ordering::Relaxed),
        ),
        (
            "connect_rejected",
            stats.connect_rejected.load(Ordering::Relaxed),
        ),
        (
            "publish_received",
            stats.publish_received.load(Ordering::Relaxed),
        ),
        (
            "dup_suppressed",
            stats.duplicate_publishes_suppressed.load(Ordering::Relaxed),
        ),
        (
            "delivered_qos0",
            stats.publish_delivered_qos0.load(Ordering::Relaxed),
        ),
        (
            "delivered_qos1",
            stats.publish_delivered_qos1.load(Ordering::Relaxed),
        ),
        ("puback", stats.puback_received.load(Ordering::Relaxed)),
        (
            "puback_unknown",
            stats.puback_unknown.load(Ordering::Relaxed),
        ),
        (
            "subscribe",
            stats.subscribe_received.load(Ordering::Relaxed),
        ),
        ("dropped", stats.publishes_dropped.load(Ordering::Relaxed)),
        ("qos2_rejected", stats.qos2_rejected.load(Ordering::Relaxed)),
    ]
    .iter()
    .map(|(k, v)| format!("{k}={v}"))
    .collect();
    parts.join(" ")
}

/// POSIX 信号处理（不引入 libc 依赖）：SIGINT=2 / SIGTERM=15。
#[cfg(unix)]
fn install_sigterm_handler() {
    extern "C" fn handler(_: i32) {
        SHUTDOWN.store(true, Ordering::SeqCst);
    }
    unsafe {
        extern "C" {
            // signal(2) 在 POSIX.1-2008 中被标记为过时，但 Linux/glibc/musl 均可用，
            // 足以满足本地测试服务「Ctrl-C 后打印统计并退出」的需求。
            fn signal(sig: i32, handler: extern "C" fn(i32)) -> usize;
        }
        signal(2, handler);
        signal(15, handler);
    }
}

#[cfg(not(unix))]
fn install_sigterm_handler() {}
