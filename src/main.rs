//! 多租户文件块配额预留服务。
//!
//! 启动示例：
//!   cargo run --release
//!   cargo run --release -- --clock injected --addr 127.0.0.1:8080 --data ./data/events.log

use quota_reservation::clock::{Clock, InjectedClock, SystemClock};
use quota_reservation::{http, store};
use std::process::ExitCode;
use std::sync::Arc;
use std::time::Duration;

struct Args {
    addr: String,
    data: String,
    clock_mode: String,
    clock_start_ms: i64,
    default_ttl_ms: i64,
    sweep_interval_ms: u64,
}

fn print_usage() {
    eprintln!(
        "quota-reservation 0.1.0\n\n\
USAGE:\n    quota-server [OPTIONS]\n\n\
OPTIONS:\n\
    --addr <HOST:PORT>            监听地址（默认 127.0.0.1:8080）\n\
    --data <PATH>                 事件日志文件路径（默认 ./data/events.log）\n\
    --clock <system|injected>     时钟模式：system=墙钟(默认)，injected=可经管理接口推进\n\
    --clock-start-ms <MS>         injected 模式的初始时钟（默认固定基准 1700000000000）\n\
    --default-ttl-ms <MS>         请求未给 ttl_ms 时的默认预留 TTL（默认 60000）\n\
    --sweep-ms <MS>               后台超时扫描间隔（默认 200）\n\
    -h, --help                    打印帮助\n"
    );
}

fn parse_args() -> Result<Args, String> {
    fn val<I: Iterator<Item = String>>(name: &str, it: &mut I) -> Result<String, String> {
        it.next()
            .ok_or_else(|| format!("missing value for {name}"))
    }

    let mut args = Args {
        addr: "127.0.0.1:8080".to_string(),
        data: "./data/events.log".to_string(),
        clock_mode: "system".to_string(),
        clock_start_ms: 1_700_000_000_000,
        default_ttl_ms: 60_000,
        sweep_interval_ms: 200,
    };
    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "-h" | "--help" => {
                print_usage();
                std::process::exit(0);
            }
            "--addr" => args.addr = val("--addr", &mut it)?,
            "--data" => args.data = val("--data", &mut it)?,
            "--clock" => {
                args.clock_mode = val("--clock", &mut it)?;
                if !matches!(args.clock_mode.as_str(), "system" | "injected") {
                    return Err("--clock must be 'system' or 'injected'".into());
                }
            }
            "--clock-start-ms" => {
                let v = val("--clock-start-ms", &mut it)?;
                args.clock_start_ms = v
                    .parse()
                    .map_err(|_| format!("invalid --clock-start-ms: {v}"))?;
            }
            "--default-ttl-ms" => {
                let v = val("--default-ttl-ms", &mut it)?;
                args.default_ttl_ms = v
                    .parse()
                    .map_err(|_| format!("invalid --default-ttl-ms: {v}"))?;
                if args.default_ttl_ms <= 0 {
                    return Err("--default-ttl-ms must be > 0".into());
                }
            }
            "--sweep-ms" => {
                let v = val("--sweep-ms", &mut it)?;
                args.sweep_interval_ms = v
                    .parse()
                    .map_err(|_| format!("invalid --sweep-ms: {v}"))?;
            }
            other => return Err(format!("unknown argument: {other}")),
        }
    }
    Ok(args)
}

#[tokio::main]
async fn main() -> ExitCode {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("error: {e}\n");
            print_usage();
            return ExitCode::FAILURE;
        }
    };

    let (clock, injected): (Arc<dyn Clock>, Option<Arc<InjectedClock>>) = match args.clock_mode
        .as_str()
    {
        "injected" => {
            let c = InjectedClock::new(args.clock_start_ms);
            (c.clone(), Some(c))
        }
        _ => (Arc::new(SystemClock), None),
    };

    let store = match store::Store::open(std::path::Path::new(&args.data), clock) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("fatal: failed to open event log '{}': {e:?}", args.data);
            return ExitCode::FAILURE;
        }
    };

    // 启动时先扫一次（进程重启后可能有在宕机期间到期的预留）。
    if let Err(e) = store.expire_due() {
        eprintln!("warning: initial expiry sweep failed: {e:?}");
    }

    // 后台超时扫描：周期释放到期预留。
    let sweep_store = store.clone();
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(Duration::from_millis(args.sweep_interval_ms));
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            ticker.tick().await;
            // 过期判定本身是幂等的，扫描失败只记录。
            if let Err(e) = sweep_store.expire_due() {
                eprintln!("warning: expiry sweep failed: {e:?}");
            }
        }
    });

    let state = http::AppState {
        store,
        injected,
        default_ttl_ms: args.default_ttl_ms,
    };
    let app = http::router(state);

    let listener = match tokio::net::TcpListener::bind(&args.addr).await {
        Ok(l) => l,
        Err(e) => {
            eprintln!("fatal: bind {} failed: {e}", args.addr);
            return ExitCode::FAILURE;
        }
    };
    eprintln!(
        "quota-server listening on http://{} (clock={}, data={}, default_ttl_ms={})",
        args.addr, args.clock_mode, args.data, args.default_ttl_ms
    );
    if let Err(e) = axum::serve(listener, app).await {
        eprintln!("fatal: server error: {e}");
        return ExitCode::FAILURE;
    }
    ExitCode::SUCCESS
}
