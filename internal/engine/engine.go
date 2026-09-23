// Package engine evaluates sensor health with three windowed rules:
//
//   - stale: silence window measured against *server receive time* and
//     heartbeats count as liveness;
//   - fixed_value: a run of consecutive equal data samples (per device type,
//     disable-able so stationary devices are not judged broken);
//   - sequence_gap: holes in the per-epoch monotonic sequence, also used to
//     recognize ordered batch backfill (old timestamps, old seq) separately
//     from device clock rollback (old timestamp, seq restart).
//
// Enter and recover thresholds are different (hysteresis), all state is
// persisted to SQLite and survives restart, and a backwards jump of the
// device sampling clock beyond the configured skew tolerance starts a new
// sequence epoch.
package engine

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"sync"
	"time"

	"sensorhealth/internal/clock"
	"sensorhealth/internal/model"
	"sensorhealth/internal/store"
)

// Phase names attached to emitted notifications.
const (
	PhaseEnter   = "ENTER"
	PhaseRecover = "RECOVER"
)

// Notification is one alert state transition, handed to the sink (webhook).
type Notification struct {
	Phase string      `json:"phase"`
	Event model.Event `json:"event"`
}

// Sink consumes committed alert transitions.
type Sink interface {
	Notify(ctx context.Context, n Notification)
}

// errUnknownType is the in-transaction sentinel for a message naming a
// device type that has no configuration; the batch reports it per item.
var errUnknownType = errors.New("unknown device type")

// ruleState is the persisted state of one rule for one device.
type ruleState struct {
	State     string     `json:"state"`
	Since     time.Time  `json:"since"`
	EventID   int64      `json:"event_id"`
	EnteredAt *time.Time `json:"entered_at,omitempty"`

	// fixed_value run tracking
	RunValue    *float64 `json:"run_value,omitempty"`
	RunCount    int      `json:"run_count"`
	RunStartSeq *int64   `json:"run_start_seq,omitempty"`
	RunStartAt  *string  `json:"run_start_at,omitempty"`
	FlatSince   *string  `json:"flat_since,omitempty"`

	// sequence gap tracking
	GapStart  *int64  `json:"gap_start,omitempty"`
	GapEnd    *int64  `json:"gap_end,omitempty"`
	Gapless   int     `json:"gapless"`
	OpenSince *string `json:"open_since,omitempty"`
	LastNote  string  `json:"last_note,omitempty"`

	// stale bookkeeping
	StaleDetail string `json:"stale_detail,omitempty"`
}

func newRuleState(at time.Time) ruleState {
	return ruleState{State: model.StateOK, Since: at}
}

// state is the full persisted per-device engine state.
type state struct {
	DeviceID   string               `json:"device_id"`
	Type       string               `json:"type"`
	Epoch      int                  `json:"epoch"`
	Rules      map[string]ruleState `json:"rules"`
	LastAnchor *time.Time           `json:"last_anchor,omitempty"` // latest device sample time seen
	UpdatedAt  time.Time            `json:"updated_at"`
}

func (s *state) rule(name string) ruleState { return s.Rules[name] }

func (s *state) setRule(name string, r ruleState) { s.Rules[name] = r }

// IngestItemResult reports what happened to one submitted message.
type IngestItemResult struct {
	Index     int    `json:"index"`
	Accepted  bool   `json:"accepted"`
	Duplicate bool   `json:"duplicate,omitempty"`
	Backfill  bool   `json:"backfill,omitempty"`
	Reason    string `json:"reason,omitempty"`
	NewEpoch  bool   `json:"new_epoch,omitempty"`
	MessageID int64  `json:"message_id,omitempty"`
}

// IngestResult is the batch outcome.
type IngestResult struct {
	ServerTime time.Time          `json:"server_time"`
	Accepted   int                `json:"accepted"`
	Rejected   int                `json:"rejected"`
	Items      []IngestItemResult `json:"items"`
}

// Engine holds all per-device runtime states in memory; they are restored
// from SQLite on startup and written back on every change.
type Engine struct {
	st   *store.Store
	clk  clock.Clock
	mu   sync.Mutex
	devs map[string]*state
	sink Sink
}

// New creates an engine and restores persisted state.
func New(ctx context.Context, st *store.Store, clk clock.Clock) (*Engine, error) {
	e := &Engine{st: st, clk: clk, devs: map[string]*state{}}
	if err := e.restore(ctx); err != nil {
		return nil, err
	}
	return e, nil
}

// SetSink installs the alert notification sink.
func (e *Engine) SetSink(s Sink) { e.sink = s }

func (e *Engine) restore(ctx context.Context) error {
	ids, err := e.st.AllStateDeviceIDs(ctx)
	if err != nil {
		return err
	}
	for _, id := range ids {
		raw, ok, err := e.st.GetState(ctx, id)
		if err != nil {
			return err
		}
		if !ok {
			continue
		}
		var s state
		if err := json.Unmarshal(raw, &s); err != nil {
			return fmt.Errorf("restore state for %s: %w", id, err)
		}
		if s.Rules == nil {
			s.Rules = map[string]ruleState{}
		}
		e.devs[id] = &s
	}
	return nil
}

func (e *Engine) emit(ctx context.Context, n Notification) {
	if e.sink != nil {
		e.sink.Notify(ctx, n)
	}
}

// ValidateMessage checks one wire message for basic well-formedness.
func ValidateMessage(m model.Message) (time.Time, error) {
	if m.DeviceID == "" {
		return time.Time{}, errors.New("device_id is required")
	}
	if m.Type == "" {
		return time.Time{}, errors.New("type is required")
	}
	st, err := time.Parse(time.RFC3339, m.SampleTime)
	if err != nil {
		return time.Time{}, fmt.Errorf("invalid sample_time %q: want RFC3339", m.SampleTime)
	}
	if !m.IsHeartbeat && m.Value == nil {
		return time.Time{}, errors.New("data message requires value (or set is_heartbeat=true)")
	}
	if m.IsHeartbeat && m.Value != nil {
		return time.Time{}, errors.New("heartbeat must not carry a value")
	}
	return st.UTC(), nil
}

// IngestOptions steers batch processing.
type IngestOptions struct {
	// Backfill marks the batch as ordered historical data delivered late
	// rather than the device's current live stream. Backfilled messages:
	//   - do NOT trigger clock-rollback detection (an old timestamp is
	//     expected — it is history, not a rebooted clock);
	//   - do NOT advance the sequence high-water mark;
	//   - are excluded from fixed-value and gap rule evaluation (replaying
	//     history must not rewrite the present);
	//   - still count as liveness for stale detection (something is
	//     delivering data now).
	Backfill bool
}

// Ingest processes a batch in submission order. Each message is evaluated
// inside its own transaction; invalid items are reported per-item without
// failing the whole batch.
func (e *Engine) Ingest(ctx context.Context, batch []model.Message, opts IngestOptions) (*IngestResult, error) {
	e.mu.Lock()
	defer e.mu.Unlock()

	res := &IngestResult{ServerTime: e.clk.Now().UTC()}
	var notifications []Notification

	for i, msg := range batch {
		item := IngestItemResult{Index: i}
		sampleAt, err := ValidateMessage(msg)
		if err != nil {
			item.Reason = err.Error()
			res.Items = append(res.Items, item)
			continue
		}
		var out *processed
		txErr := e.st.RunTx(ctx, func(tx *sql.Tx) error {
			// Configuration and device are resolved inside tx: the pool
			// has a single connection, so all DB access must use tx.
			cfg, err := store.GetDeviceTypeTx(ctx, tx, msg.Type)
			if err != nil {
				if errors.Is(err, sql.ErrNoRows) {
					return errUnknownType
				}
				return err
			}
			recvAt := e.clk.Now().UTC()
			sm := model.StoredMessage{Message: msg, RecvAt: recvAt}
			out, err = e.processOne(ctx, tx, sm, sampleAt, cfg, opts.Backfill)
			return err
		})
		if errors.Is(txErr, errUnknownType) {
			item.Reason = "unknown device type: " + msg.Type
			res.Items = append(res.Items, item)
			continue
		}
		if txErr != nil {
			return nil, txErr
		}
		item.Accepted = true
		item.MessageID = out.messageID
		item.Duplicate = out.duplicate
		item.Backfill = out.backfill
		item.NewEpoch = out.newEpoch
		res.Items = append(res.Items, item)
		notifications = append(notifications, out.notifications...)
	}
	acc := 0
	for _, it := range res.Items {
		if it.Accepted {
			acc++
		}
	}
	res.Accepted = acc
	res.Rejected = len(res.Items) - acc

	// Notify after commit: if delivery fails, the event itself is durable
	// and the webhook layer records its own delivery attempts.
	for _, n := range notifications {
		e.emit(ctx, n)
	}
	return res, nil
}

type processed struct {
	messageID     int64
	duplicate     bool
	backfill      bool
	newEpoch      bool
	notifications []Notification
}

func (e *Engine) loadOrInitState(tx *sql.Tx, ctx context.Context, dev model.Device, cfg model.DeviceType) (*state, error) {
	if s, ok := e.devs[dev.ID]; ok {
		s.Type = dev.Type
		return s, nil
	}
	s := &state{
		DeviceID: dev.ID,
		Type:     dev.Type,
		Epoch:    dev.Epoch,
		Rules: map[string]ruleState{
			model.RuleStale: newRuleState(e.clk.Now().UTC()),
			model.RuleFixed: newRuleState(e.clk.Now().UTC()),
			model.RuleGap:   newRuleState(e.clk.Now().UTC()),
		},
	}
	if dev.LastSample != nil {
		t := *dev.LastSample
		s.LastAnchor = &t
	}
	e.devs[dev.ID] = s
	return s, nil
}

func (e *Engine) processOne(ctx context.Context, tx *sql.Tx, sm model.StoredMessage,
	sampleAt time.Time, cfg model.DeviceType, backfill bool) (*processed, error) {
	out := &processed{backfill: backfill}

	dev, err := store.GetDeviceTx(ctx, tx, sm.DeviceID)
	isNew := false
	if errors.Is(err, sql.ErrNoRows) {
		dev = model.Device{
			ID: sm.DeviceID, Type: sm.Type, Epoch: 0,
			LastRecvAt: sm.RecvAt, CreatedAt: sm.RecvAt,
		}
		isNew = true
	} else if err != nil {
		return nil, err
	} else {
		dev.Type = sm.Type // latest wire type wins
	}

	s, err := e.loadOrInitState(tx, ctx, dev, cfg)
	if err != nil {
		return nil, err
	}

	// Store the raw message first so every alert can cite its interval.
	mid, err := store.InsertMessageTx(ctx, tx, sm)
	if err != nil {
		return nil, err
	}
	out.messageID = mid
	now := sm.RecvAt

	// Liveness projection is updated for every received message, live or
	// historical: receiving a backfill batch right now proves the link is up.
	dev.LastRecvAt = now
	if sm.IsHeartbeat {
		dev.LastHeartbeat = &sampleAt
	} else {
		dev.LastSample = &sampleAt
	}

	if backfill {
		// Historical replay: no rollback detection, no HW mark movement, no
		// fixed/gap evaluation. A recovered open stale alert still clears
		// because data is arriving now.
		if err := e.recoverStaleOnMessage(ctx, tx, s, sm, cfg.Version, now, out); err != nil {
			return nil, err
		}
		if isNew {
			if err := store.CreateDeviceTx(ctx, tx, &dev); err != nil {
				return nil, err
			}
		} else {
			if err := store.UpsertDeviceTx(ctx, tx, &dev); err != nil {
				return nil, err
			}
		}
		s.UpdatedAt = now
		if err := e.persist(ctx, tx, s, now); err != nil {
			return nil, err
		}
		return out, nil
	}

	// --- live path below ---

	// Device clock rollback: sample time jumped backwards beyond tolerance.
	// Heartbeats participate too — a heartbeat carrying an old device clock
	// after newer samples means the device rebooted/clock-synced.
	rollback := false
	if s.LastAnchor != nil {
		delta := s.LastAnchor.Sub(sampleAt)
		tol := time.Duration(cfg.ClockSkewTolMs) * time.Millisecond
		if delta > tol {
			rollback = true
		}
	}
	if rollback {
		if err := e.beginNewEpoch(ctx, tx, s, &dev, sampleAt, sm, cfg, now, out); err != nil {
			return nil, err
		}
	} else {
		if t := sampleAt; s.LastAnchor == nil || sampleAt.After(*s.LastAnchor) {
			s.LastAnchor = &t
		}
	}

	if !sm.IsHeartbeat && sm.Seq != nil {
		if !dev.HasSeq {
			dev.HasSeq = true
			dev.LastSeq = *sm.Seq
			s.Epoch = dev.Epoch
		} else {
			switch {
			case *sm.Seq == dev.LastSeq && !rollback:
				// Duplicate replay of the last seq.
				out.duplicate = true
			case *sm.Seq < dev.LastSeq && !rollback:
				// Older sequence arriving late: ordered batch backfill.
				// It does NOT heal gaps and does not advance the HW mark.
				out.backfill = true
				if r := s.rule(model.RuleGap); r.State == model.StateOK {
					r.Gapless = 0
					s.setRule(model.RuleGap, r)
				}
			case *sm.Seq > dev.LastSeq && !rollback:
				if err := e.handleForwardSeq(ctx, tx, s, &dev, sm, cfg, now, out); err != nil {
					return nil, err
				}
			}
		}
	}

	// Fixed-value detection runs only on non-heartbeat data messages.
	if !sm.IsHeartbeat && sm.Value != nil {
		if err := e.handleFixedValue(ctx, tx, s, sm, cfg, now, out); err != nil {
			return nil, err
		}
	}

	// Stale evaluation: any received message (data or heartbeat) proves
	// liveness, so an open stale alert is cleared immediately (recovery is
	// possible because the current message is, by definition, within the
	// tighter recovery window of now). Absence of messages is handled by
	// Sweep.
	if err := e.recoverStaleOnMessage(ctx, tx, s, sm, cfg.Version, now, out); err != nil {
		return nil, err
	}

	if isNew {
		if err := store.CreateDeviceTx(ctx, tx, &dev); err != nil {
			return nil, err
		}
	} else {
		if err := store.UpsertDeviceTx(ctx, tx, &dev); err != nil {
			return nil, err
		}
	}
	s.UpdatedAt = now
	if err := e.persist(ctx, tx, s, now); err != nil {
		return nil, err
	}
	return out, nil
}

// beginNewEpoch starts a fresh sequence epoch after a device clock rollback.
func (e *Engine) beginNewEpoch(ctx context.Context, tx *sql.Tx, s *state, dev *model.Device,
	sampleAt time.Time, sm model.StoredMessage, cfg model.DeviceType, now time.Time,
	out *processed) error {
	s.Epoch++
	dev.Epoch = s.Epoch
	dev.LastSeq = 0
	if sm.Seq != nil {
		dev.HasSeq = true
		dev.LastSeq = *sm.Seq
	} else {
		dev.HasSeq = false
	}
	t := sampleAt
	s.LastAnchor = &t
	out.newEpoch = true

	// An open gap alert refers to a sequence range in the previous epoch;
	// the rollback invalidates it. Recover it explicitly.
	gr := s.rule(model.RuleGap)
	if gr.State == model.StateAlert {
		if err := e.recoverEvent(ctx, tx, s, model.RuleGap, sm, cfg.Version, now,
			"device clock rolled back; new sequence epoch started", out); err != nil {
			return err
		}
		gr = s.rule(model.RuleGap)
	}
	gr.GapStart, gr.GapEnd, gr.Gapless, gr.LastNote = nil, nil, 0, ""
	gr.OpenSince = nil
	s.setRule(model.RuleGap, gr)

	// Reset fixed-value run bookkeeping. A long constant reading from the
	// pre-reboot device can't be carried across the epoch boundary; if the
	// value really stays constant, the run builds up again from zero.
	fr := s.rule(model.RuleFixed)
	fr.RunCount, fr.RunValue, fr.RunStartSeq, fr.RunStartAt, fr.FlatSince = 0, nil, nil, nil, nil
	s.setRule(model.RuleFixed, fr)
	return nil
}

// handleForwardSeq updates gap state for a strictly-forward new sequence.
func (e *Engine) handleForwardSeq(ctx context.Context, tx *sql.Tx, s *state,
	dev *model.Device, sm model.StoredMessage, cfg model.DeviceType,
	now time.Time, out *processed) error {
	newSeq := *sm.Seq
	gapLen := newSeq - dev.LastSeq - 1
	dev.LastSeq = newSeq
	gr := s.rule(model.RuleGap)

	if gapLen > 0 {
		missingStart := newSeq - gapLen
		missingEnd := newSeq - 1
		gr.GapStart, gr.GapEnd = &missingStart, &missingEnd
		gr.Gapless = 0
		note := fmt.Sprintf("missing seq %d..%d before seq %d", missingStart, missingEnd, newSeq)
		gr.LastNote = note
		if gr.State != model.StateAlert {
			gr.OpenSince = strPtr(now.Format(time.RFC3339Nano))
			ev := model.Event{
				DeviceID: s.DeviceID, DeviceType: s.Type, Rule: model.RuleGap,
				Open: model.EventOpen, TriggeredAt: now,
				ConfigVersion: cfg.Version,
				StartSeq:      sm.Seq, EndSeq: sm.Seq,
				StartSample: parseSample(sm.SampleTime), EndSample: parseSample(sm.SampleTime),
				GapStart: &missingStart, GapEnd: &missingEnd, Note: note,
			}
			if err := store.InsertEventTx(ctx, tx, &ev); err != nil {
				return err
			}
			gr.State, gr.Since, gr.EventID, gr.StaleDetail = model.StateAlert, now, ev.ID, note
			out.notifications = append(out.notifications, Notification{Phase: PhaseEnter, Event: ev})
		} else {
			// Widen the missing range of the already-open alert.
			if err := store.UpdateEventGapTx(ctx, tx, gr.EventID, gr.GapStart, gr.GapEnd); err != nil {
				return err
			}
			if err := e.updateEventSampleTx(ctx, tx, gr.EventID, sm.Seq, sm.SampleTime, note); err != nil {
				return err
			}
		}
		// Persist the (possibly newly entered) alert state back into the
		// device state; without this the next message would re-read the
		// stale pre-gap state and never count gap-free recovery samples.
		s.setRule(model.RuleGap, gr)
	} else {
		// Clean consecutive sample.
		gr.GapStart, gr.GapEnd = nil, nil
		if gr.State == model.StateAlert {
			gr.Gapless++
			if gr.Gapless >= cfg.GapRecoverGapless {
				if err := e.recoverEvent(ctx, tx, s, model.RuleGap, sm, cfg.Version, now,
					fmt.Sprintf("%d consecutive gap-free samples", gr.Gapless), out); err != nil {
					return err
				}
				gr = s.rule(model.RuleGap)
				gr.Gapless = 0
				gr.OpenSince = nil
				s.setRule(model.RuleGap, gr)
			} else {
				s.setRule(model.RuleGap, gr)
			}
		} else {
			gr.Gapless = 0
			s.setRule(model.RuleGap, gr)
		}
	}
	return nil
}

// handleFixedValue tracks runs of equal values with enter/exit thresholds.
func (e *Engine) handleFixedValue(ctx context.Context, tx *sql.Tx, s *state,
	sm model.StoredMessage, cfg model.DeviceType, now time.Time,
	out *processed) error {
	// Type disables this rule (legitimately stationary devices).
	if cfg.FixedWindowCount == 0 {
		return nil
	}
	fr := s.rule(model.RuleFixed)
	v := *sm.Value
	tol := cfg.FixedTolerance

	equal := fr.RunValue != nil && math.Abs(v-*fr.RunValue) <= tol
	if !equal {
		old := fr.RunValue
		// A genuinely changing value proves the sensor is alive: recover
		// any fixed-value alert immediately (recover threshold is "value
		// changed", a different rule from the enter threshold).
		if fr.State == model.StateAlert {
			note := "value changed"
			if old != nil {
				note = fmt.Sprintf("value changed: %v -> %v", *old, v)
			}
			if err := e.recoverEvent(ctx, tx, s, model.RuleFixed, sm, cfg.Version, now, note, out); err != nil {
				return err
			}
		}
		// re-read: recoverEvent stored its own copy, mutating the stale
		// local fr afterwards would clobber the OK state.
		fr = s.rule(model.RuleFixed)
		fr.RunValue = &v
		fr.RunCount = 1
		fr.RunStartSeq = sm.Seq
		st := sm.SampleTime
		fr.RunStartAt = &st
		fr.FlatSince = nil
		s.setRule(model.RuleFixed, fr)
		return nil
	}

	fr.RunCount++
	if fr.State != model.StateAlert && fr.RunCount >= cfg.FixedWindowCount {
		// Enter fixed_value alert. The trigger interval is the whole equal
		// run, from its first sample to the threshold-hitting sample.
		if fr.FlatSince == nil {
			flat := now
			fr.FlatSince = strPtr(flat.Format(time.RFC3339Nano))
		}
		var startSeq *int64
		if fr.RunStartSeq != nil {
			v := *fr.RunStartSeq
			startSeq = &v
		}
		var startSample *time.Time
		if fr.RunStartAt != nil {
			if t, err := time.Parse(time.RFC3339, *fr.RunStartAt); err == nil {
				startSample = &t
			}
		}
		note := fmt.Sprintf("%d consecutive equal samples (value=%v, tolerance=%v)",
			fr.RunCount, v, tol)
		ev := model.Event{
			DeviceID: s.DeviceID, DeviceType: s.Type, Rule: model.RuleFixed,
			Open: model.EventOpen, TriggeredAt: now, ConfigVersion: cfg.Version,
			StartSeq: startSeq, EndSeq: sm.Seq,
			StartSample: startSample, EndSample: parseSample(sm.SampleTime),
			Note: note,
		}
		if err := store.InsertEventTx(ctx, tx, &ev); err != nil {
			return err
		}
		fr.State, fr.Since, fr.EventID, fr.StaleDetail = model.StateAlert, now, ev.ID, note
		out.notifications = append(out.notifications, Notification{Phase: PhaseEnter, Event: ev})
	} else if fr.State == model.StateAlert {
		note := fmt.Sprintf("still constant at %v (%d samples)", v, fr.RunCount)
		if err := e.updateEventSampleTx(ctx, tx, fr.EventID, sm.Seq, sm.SampleTime, note); err != nil {
			return err
		}
		fr.StaleDetail = note
	}
	s.setRule(model.RuleFixed, fr)
	return nil
}

// recoverStaleOnMessage clears an open stale alert when a fresh message
// (data or heartbeat) arrives. Its receive time is now, so the age is zero
// and trivially inside the tighter recovery window.
func (e *Engine) recoverStaleOnMessage(ctx context.Context, tx *sql.Tx, s *state,
	sm model.StoredMessage, cfgVersion int, now time.Time, out *processed) error {
	r := s.rule(model.RuleStale)
	if r.State != model.StateAlert {
		return nil
	}
	if err := e.recoverEvent(ctx, tx, s, model.RuleStale, sm, cfgVersion, now,
		"liveness restored by "+messageKind(sm), out); err != nil {
		return err
	}
	r = s.rule(model.RuleStale)
	r.StaleDetail = ""
	s.setRule(model.RuleStale, r)
	return nil
}

func messageKind(sm model.StoredMessage) string {
	if sm.IsHeartbeat {
		return "heartbeat"
	}
	return "data message"
}

// Sweep marks devices stale whose last received message is older than the
// enter window. It is called periodically and (deterministically) after the
// virtual debug clock is advanced.
func (e *Engine) Sweep(ctx context.Context) (int, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	now := e.clk.Now().UTC()
	fired := 0
	var notifications []Notification

	for id, s := range e.devs {
		cfg, err := e.st.GetDeviceType(ctx, s.Type)
		if err != nil {
			return fired, err
		}
		dev, err := e.st.GetDevice(ctx, id)
		if err != nil {
			if errors.Is(err, sql.ErrNoRows) {
				continue
			}
			return fired, err
		}
		age := now.Sub(dev.LastRecvAt)
		r := s.rule(model.RuleStale)
		needEnter := age > time.Duration(cfg.StaleEnterSec)*time.Second
		if needEnter && r.State != model.StateAlert {
			var entered model.Event
			txErr := e.st.RunTx(ctx, func(tx *sql.Tx) error {
				note := fmt.Sprintf("no message for %s (enter %ds, recover %ds)",
					age.Round(time.Millisecond), cfg.StaleEnterSec, cfg.StaleRecoverSec)
				ev := model.Event{
					DeviceID: id, DeviceType: s.Type, Rule: model.RuleStale,
					Open: model.EventOpen, TriggeredAt: now,
					ConfigVersion: cfg.Version, Note: note,
				}
				if dev.LastSample != nil {
					ev.EndSample = dev.LastSample
					ev.StartSample = dev.LastSample
				}
				if dev.HasSeq {
					seq := dev.LastSeq
					ev.StartSeq, ev.EndSeq = &seq, &seq
				}
				if err := store.InsertEventTx(ctx, tx, &ev); err != nil {
					return err
				}
				r.State, r.Since, r.EventID, r.StaleDetail = model.StateAlert, now, ev.ID, note
				s.setRule(model.RuleStale, r)
				s.UpdatedAt = now
				if err := e.persist(ctx, tx, s, now); err != nil {
					return err
				}
				entered = ev
				return nil
			})
			if txErr != nil {
				return fired, txErr
			}
			fired++
			notifications = append(notifications, Notification{Phase: PhaseEnter, Event: entered})
		}
	}
	for _, n := range notifications {
		e.emit(ctx, n)
	}
	return fired, nil
}

// recoverEvent closes the open event for rule and emits RECOVER. The event's
// trigger sample interval is finalized with the sample that caused recovery.
func (e *Engine) recoverEvent(ctx context.Context, tx *sql.Tx, s *state, rule string,
	sm model.StoredMessage, cfgVersion int, now time.Time, note string,
	out *processed) error {
	ev, err := store.GetOpenEventTx(ctx, tx, s.DeviceID, rule)
	if err != nil {
		return err
	}
	r := s.rule(rule)
	if ev == nil {
		// Defensive: state says alert but row missing. Reset to OK.
		r.State, r.EventID, r.StaleDetail = model.StateOK, 0, ""
		r.Since = now
		s.setRule(rule, r)
		return nil
	}
	ev.EndSeq = sm.Seq
	ev.EndSample = parseSample(sm.SampleTime)
	ev.Note = note
	ver := cfgVersion
	ev.RecoveryConfigVersion = &ver
	if err := store.CloseEventTx(ctx, tx, ev, now, cfgVersion); err != nil {
		return err
	}
	r.State = model.StateOK
	r.Since = now
	r.EventID = 0
	r.StaleDetail = note
	s.setRule(rule, r)
	closed := *ev
	closed.Open = false
	closed.RecoveredAt = &now
	out.notifications = append(out.notifications, Notification{Phase: PhaseRecover, Event: closed})
	return nil
}

func (e *Engine) updateEventSampleTx(ctx context.Context, tx *sql.Tx, id int64,
	seq *int64, sample string, note string) error {
	_, err := tx.ExecContext(ctx,
		`UPDATE events SET end_seq=?, end_sample=?, note=? WHERE id=? AND open=1`,
		seq, sample, note, id)
	return err
}

func (e *Engine) persist(ctx context.Context, tx *sql.Tx, s *state, at time.Time) error {
	raw, err := json.Marshal(s)
	if err != nil {
		return err
	}
	return store.PutStateTx(ctx, tx, s.DeviceID, raw, at)
}

func parseSample(s string) *time.Time {
	t, err := time.Parse(time.RFC3339, s)
	if err != nil {
		return nil
	}
	return &t
}

func strPtr(s string) *string { return &s }
