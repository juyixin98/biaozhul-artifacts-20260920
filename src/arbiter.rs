use crate::config::Limits;
use crate::models::*;

/// 纯函数仲裁：给定世界视图（命令、急停事件）与“当前时刻”，产出一次决策。
/// 不触碰数据库、不触碰时钟，因此可以确定性地单测。
pub fn evaluate(world: &WorldView, now_ms: i64, limits: &Limits) -> DecisionOutcome {
    // 1) 急停锁存：以最近一条急停事件为准（trigger -> 锁存；clear -> 解锁）。
    let latest_estop = world.estop_events.last();
    let estop_latched = matches!(latest_estop.map(|e| e.action.as_str()), Some("trigger"));
    // 最近一次急停事件的接收时刻构成“栅栏”：所有在此之前（含）接收的运动命令
    // 在 clear 后全部失效，强制要求重新收到新鲜命令。
    let fence_ms = latest_estop.map(|e| e.received_ms);

    // 2) 最近一次遥控命令决定遥控窗口；急停栅栏之前收到的遥控命令一律不算数，
    //    栅栏之后必须收到新鲜遥控才能重新占据遥控窗口。
    let latest_rc = world
        .commands
        .iter()
        .filter(|c| c.source_kind == "rc")
        .filter(|c| fence_ms.is_none_or(|f| c.received_ms > f))
        .max_by(|a, b| a.received_ms.cmp(&b.received_ms).then(a.seq.cmp(&b.seq)));
    let rc_alive = latest_rc.is_some_and(|c| c.deadline_ms > now_ms);
    let rc_window_end = latest_rc.map(|c| c.deadline_ms);

    let mut suppressed = Vec::new();
    let mut candidates: Vec<&Command> = Vec::new();

    for cmd in &world.commands {
        // 2a) 锁存期间，一切运动命令被抑制（解除后再由栅栏判失效）。
        if estop_latched {
            suppressed.push(mk_sup(
                cmd,
                "suppressed_while_estop_latched",
                "急停处于锁存状态，仅输出零速决策",
            ));
            continue;
        }
        // 2b) 急停栅栏：clear 之后，最近一次急停事件（按下或解除）之前收到的命令一律失效，
        //     强制要求重新下发新鲜命令。
        if fence_ms.is_some_and(|f| cmd.received_ms <= f) {
            suppressed.push(mk_sup(
                cmd,
                "invalidated_by_estop_event",
                "命令接收于最近一次急停事件（按下或解除）之前，急停解除后必须重新下发新鲜命令",
            ));
            continue;
        }
        // 2c) 租约到期。
        if now_ms > cmd.deadline_ms {
            let reason = if cmd.source_kind == "rc" {
                "rc_lease_lost"
            } else {
                "lease_expired"
            };
            suppressed.push(mk_sup(cmd, reason, "租约已到期"));
            continue;
        }
        // 2d) 遥控/自主的窗口规则。
        if cmd.source_kind == "rc" {
            if !rc_alive {
                // 理论上不可达（deadline 未过即 alive），防御性保留。
                suppressed.push(mk_sup(cmd, "rc_lease_lost", "遥控租约未存活"));
                continue;
            }
            let is_latest_rc = latest_rc.is_some_and(|c| std::ptr::eq(c, cmd));
            if !is_latest_rc {
                suppressed.push(mk_sup(
                    cmd,
                    "superseded_by_newer_rc",
                    "已被更新的遥控命令取代",
                ));
                continue;
            }
        } else if cmd.source_kind == "autonomous" {
            if rc_alive {
                suppressed.push(mk_sup(
                    cmd,
                    "higher_priority_active",
                    "遥控命令租约存活，自主命令被更高优先级来源抑制",
                ));
                continue;
            }
            // 遥控失联后：在“上一段遥控窗口结束时刻”之前接收的自主命令不得恢复。
            if rc_window_end.is_some_and(|end| cmd.received_ms < end) {
                suppressed.push(mk_sup(cmd, "stale_after_remote_loss",
                    "遥控失联后不得恢复旧自主命令：该命令接收于上一遥控窗口结束之前，需新鲜自主命令"));
                continue;
            }
        }
        candidates.push(cmd);
    }

    // 3) 选择：急停优先锁存；否则按确定性全序挑选候选。
    let chosen = if estop_latched {
        let ev = latest_estop.expect("latched implies an event");
        Some(Chosen {
            source: ev.source.clone(),
            source_kind: "estop".into(),
            seq: ev.seq,
            vx: 0.0,
            vy: 0.0,
            omega: 0.0,
            reason: "estop_triggered".into(),
            clamped: None,
            deadline_ms: None,
        })
    } else if let Some(win) = candidates.iter().copied().max_by(candidate_order) {
        let (vx, vy, omega, clamped) = clamp_velocity(win.vx, win.vy, win.omega, limits);
        let reason = if win.source_kind == "rc" {
            "rc_fresh"
        } else {
            // 存在过遥控窗口且自主命令合格，说明是失联后的新鲜自主命令。
            if rc_window_end.is_some() {
                "autonomous_fresh_after_rc_loss"
            } else {
                "autonomous_active"
            }
        };
        Some(Chosen {
            source: win.source.clone(),
            source_kind: win.source_kind.clone(),
            seq: win.seq,
            vx,
            vy,
            omega,
            reason: reason.into(),
            clamped,
            deadline_ms: Some(win.deadline_ms),
        })
    } else {
        // 无合格命令：安全零速（仅决策记录，不驱动电机）。
        Some(Chosen {
            source: "none".into(),
            source_kind: "none".into(),
            seq: -1,
            vx: 0.0,
            vy: 0.0,
            omega: 0.0,
            reason: "no_active_command".into(),
            clamped: None,
            deadline_ms: None,
        })
    };

    // 4) 各来源状态快照。
    let mut sources: Vec<&str> = world.commands.iter().map(|c| c.source.as_str()).collect();
    sources.sort();
    sources.dedup();
    let source_states = sources
        .into_iter()
        .map(|s| {
            let latest = world
                .commands
                .iter()
                .filter(|c| c.source == s)
                .max_by_key(|c| c.seq)
                .unwrap();
            SourceState {
                source: latest.source.clone(),
                source_kind: latest.source_kind.clone(),
                latest_seq: Some(latest.seq),
                latest_deadline_ms: Some(latest.deadline_ms),
                lease_alive: latest.deadline_ms > now_ms,
            }
        })
        .collect();

    // 抑制列表也按确定性顺序输出（来源、序号）。
    suppressed.sort_by(|a, b| a.source.cmp(&b.source).then(a.seq.cmp(&b.seq)));

    DecisionOutcome {
        at_ms: now_ms,
        estop_latched,
        chosen,
        suppressed,
        source_states,
    }
}

fn mk_sup(cmd: &Command, reason: &str, detail: &str) -> Suppressed {
    Suppressed {
        source: cmd.source.clone(),
        source_kind: cmd.source_kind.clone(),
        seq: cmd.seq,
        reason: reason.into(),
        detail: detail.into(),
    }
}

/// 候选全序（确定性）：
/// 1. 来源优先级：遥控 > 自主；
/// 2. 租约到期时刻更晚者优先（更“新鲜”）；
/// 3. 接收时刻更晚者优先；
/// 4. 序号更大者优先；
/// 5. 来源名字典序兜底（同刻竞争永不出现“随机胜出”）。
fn candidate_order<'a>(a: &&'a Command, b: &&'a Command) -> std::cmp::Ordering {
    let prio = |k: &str| if k == "rc" { 1 } else { 0 };
    prio(&a.source_kind)
        .cmp(&prio(&b.source_kind))
        .then(a.deadline_ms.cmp(&b.deadline_ms))
        .then(a.received_ms.cmp(&b.received_ms))
        .then(a.seq.cmp(&b.seq))
        .then(b.source.cmp(&a.source)) // 字典序小者优先 => 反向比较
}

fn clamp_velocity(
    vx: f64,
    vy: f64,
    omega: f64,
    lim: &Limits,
) -> (f64, f64, f64, Option<ClampInfo>) {
    let cv = |v: f64, lim: f64| v.clamp(-lim.abs(), lim.abs());
    let (ex, ey, ew) = (cv(vx, lim.vx), cv(vy, lim.vy), cv(omega, lim.omega));
    let clamped = if ex != vx || ey != vy || ew != omega {
        Some(ClampInfo {
            requested: [vx, vy, omega],
            emitted: [ex, ey, ew],
        })
    } else {
        None
    };
    (ex, ey, ew, clamped)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cmd(
        source: &str,
        kind: &str,
        seq: i64,
        v: f64,
        issue: i64,
        lease: i64,
        recv: i64,
    ) -> Command {
        Command {
            source: source.into(),
            source_kind: kind.into(),
            seq,
            nonce: format!("n-{source}-{seq}"),
            vx: v,
            vy: 0.0,
            omega: 0.0,
            issue_ms: issue,
            deadline_ms: issue + lease,
            received_ms: recv,
        }
    }

    fn ev(source: &str, seq: i64, action: &str, recv: i64) -> EstopEvent {
        EstopEvent {
            source: source.into(),
            seq,
            action: action.into(),
            received_ms: recv,
        }
    }

    const LIM: Limits = Limits {
        vx: 1.5,
        vy: 1.0,
        omega: 2.0,
    };

    #[test]
    fn autonomous_active_when_no_rc() {
        let w = WorldView {
            commands: vec![cmd("auto", "autonomous", 1, 0.5, 100, 1000, 110)],
            estop_events: vec![],
        };
        let d = evaluate(&w, 500, &LIM);
        let c = d.chosen.unwrap();
        assert_eq!(c.source, "auto");
        assert_eq!(c.reason, "autonomous_active");
        assert!(!d.estop_latched);
        assert!(d.suppressed.is_empty());
    }

    #[test]
    fn rc_supersedes_autonomous_while_alive() {
        let w = WorldView {
            commands: vec![
                cmd("auto", "autonomous", 1, 0.5, 100, 5000, 110),
                cmd("rc1", "rc", 7, 1.0, 200, 5000, 210),
            ],
            estop_events: vec![],
        };
        let d = evaluate(&w, 1000, &LIM);
        assert_eq!(d.chosen.unwrap().source, "rc1");
        assert_eq!(d.suppressed[0].source, "auto");
        assert_eq!(d.suppressed[0].reason, "higher_priority_active");
    }

    #[test]
    fn stale_autonomous_is_not_restored_after_rc_loss() {
        // auto 在遥控窗口期间接收；rc 在 t=2000 失联；t=3000 仲裁。
        let w = WorldView {
            commands: vec![
                cmd("auto", "autonomous", 1, 0.5, 100, 10_000, 150),
                cmd("rc1", "rc", 7, 1.0, 200, 1800, 210),
            ],
            estop_events: vec![],
        };
        let d = evaluate(&w, 3000, &LIM);
        // rc 已失连，auto 是旧命令 => 零速，两个抑制原因都在。
        let c = d.chosen.unwrap();
        assert_eq!(c.reason, "no_active_command");
        assert_eq!(c.vx, 0.0);
        let reasons: Vec<_> = d
            .suppressed
            .iter()
            .map(|s| (s.source.as_str(), s.reason.as_str()))
            .collect();
        assert!(reasons.contains(&("rc1", "rc_lease_lost")));
        assert!(reasons.contains(&("auto", "stale_after_remote_loss")));
    }

    #[test]
    fn fresh_autonomous_takes_over_after_rc_loss() {
        let mut w = WorldView {
            commands: vec![
                cmd("auto", "autonomous", 1, 0.5, 100, 10_000, 150),
                cmd("rc1", "rc", 7, 1.0, 200, 1800, 210),
            ],
            estop_events: vec![],
        };
        // 失联后（窗口结束于 2000）收到新鲜自主命令。
        w.commands
            .push(cmd("auto", "autonomous", 2, 0.7, 2500, 5000, 2510));
        let d = evaluate(&w, 3000, &LIM);
        let c = d.chosen.unwrap();
        assert_eq!(c.source, "auto");
        assert_eq!(c.seq, 2);
        assert_eq!(c.reason, "autonomous_fresh_after_rc_loss");
    }

    #[test]
    fn estop_latches_and_outputs_zero_then_clear_invalidates_old_commands() {
        let mut w = WorldView {
            commands: vec![cmd("auto", "autonomous", 1, 0.5, 100, 100_000, 110)],
            estop_events: vec![ev("estop", 1, "trigger", 400)],
        };
        let d = evaluate(&w, 1000, &LIM);
        assert!(d.estop_latched);
        let c = d.chosen.unwrap();
        assert_eq!(c.reason, "estop_triggered");
        assert_eq!((c.vx, c.vy, c.omega), (0.0, 0.0, 0.0));
        assert_eq!(d.suppressed[0].reason, "suppressed_while_estop_latched");

        // 锁存期间到达的运动命令同样不生效。
        w.commands
            .push(cmd("rc1", "rc", 9, 1.0, 1100, 100_000, 1110));
        let d = evaluate(&w, 2000, &LIM);
        assert_eq!(d.chosen.unwrap().reason, "estop_triggered");
        assert_eq!(d.suppressed.len(), 2);

        // 解除：两条旧命令都在栅栏之前，全部失效，需要新鲜命令。
        w.estop_events.push(ev("estop", 2, "clear", 3000));
        let d = evaluate(&w, 3100, &LIM);
        assert!(!d.estop_latched);
        assert_eq!(d.chosen.unwrap().reason, "no_active_command");
        for s in &d.suppressed {
            assert_eq!(s.reason, "invalidated_by_estop_event");
        }

        // 新鲜自主命令恢复。
        w.commands
            .push(cmd("auto", "autonomous", 2, 0.4, 3200, 5000, 3210));
        let d = evaluate(&w, 3300, &LIM);
        assert_eq!(d.chosen.unwrap().source, "auto");
    }

    #[test]
    fn same_instant_competition_is_deterministic() {
        // 两个同类来源、同刻、同租期：先按优先级（不适用），再按来源字典序，
        // "auto-a" 必须确定性胜出。
        let make = |s: &str| cmd(s, "autonomous", 1, 1.0, 0, 1000, 100);
        let w = WorldView {
            commands: vec![make("auto-b"), make("auto-a")],
            estop_events: vec![],
        };
        let d1 = evaluate(&w, 500, &LIM);
        // 多跑几次，确认与遍历顺序无关。
        let w2 = WorldView {
            commands: vec![make("auto-a"), make("auto-b")],
            estop_events: vec![],
        };
        let d2 = evaluate(&w2, 500, &LIM);
        assert_eq!(d1.chosen.as_ref().unwrap().source, "auto-a");
        assert_eq!(d2.chosen.as_ref().unwrap().source, "auto-a");

        // rc 与自主同刻竞争，rc 永远优先。
        let w3 = WorldView {
            commands: vec![
                cmd("auto-a", "autonomous", 1, 1.0, 0, 1000, 100),
                cmd("rc1", "rc", 1, 1.0, 0, 1000, 100),
            ],
            estop_events: vec![],
        };
        assert_eq!(evaluate(&w3, 500, &LIM).chosen.unwrap().source, "rc1");
    }

    #[test]
    fn velocity_is_clamped() {
        let w = WorldView {
            commands: vec![cmd("auto", "autonomous", 1, 9.0, 0, 1000, 10)],
            estop_events: vec![],
        };
        let d = evaluate(&w, 100, &LIM);
        let c = d.chosen.unwrap();
        assert_eq!(c.vx, 1.5);
        let ci = c.clamped.unwrap();
        assert_eq!(ci.requested[0], 9.0);
        assert_eq!(ci.emitted[0], 1.5);
    }
}
