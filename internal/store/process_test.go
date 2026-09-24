package store

import (
	"context"
	"fmt"
	"os"
	"testing"
	"time"
)

// dsnFromEnv returns the test DSN; tests skip when no PostgreSQL is reachable.
func dsnFromEnv() string {
	if d := os.Getenv("PG_DSN"); d != "" {
		return d
	}
	return "postgres://mqttredel:mqttredel@127.0.0.1:55433/mqttredel?sslmode=disable"
}

func openTestStore(t *testing.T) context.Context {
	t.Helper()
	// Short probe timeout: when no PostgreSQL is present the tests skip
	// promptly instead of waiting for the service's full connect timeout.
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := Open(ctx, dsnFromEnv()); err != nil {
		t.Skipf("postgresql not available, skipping integration test: %v", err)
	}
	t.Cleanup(Close)
	// Clean slate for deterministic counts.
	if _, err := DB().Exec(context.Background(),
		`TRUNCATE raw_messages, events, device_boots, device_state, quarantine, devices RESTART IDENTITY`); err != nil {
		t.Skipf("cannot truncate test tables: %v", err)
	}
	return context.Background()
}

func mkEvent(boot, seq int64, value float64) EventInput {
	return EventInput{
		DeviceID:   "dev-t",
		BootGen:    boot,
		Seq:        seq,
		Value:      value,
		MeasuredAt: time.UnixMilli(1_700_000_000_000 + seq*1000).UTC(),
		Topic:      "telemetry/dev-t",
		PacketID:   uint16(seq),
		PayloadRaw: []byte(fmt.Sprintf(`{"seq":%d}`, seq)),
	}
}

// TestProcessEventDedup is THE core invariant: two deliveries of the same
// business key commit without error and leave exactly one business row.
func TestProcessEventDedup(t *testing.T) {
	ctx := openTestStore(t)
	in := mkEvent(1, 1, 10.5)

	if err := InTx(ctx, func(tx tx) error {
		r, err := ProcessEvent(ctx, tx, in, "")
		if err != nil {
			return err
		}
		if r != ResultInserted {
			t.Fatalf("first delivery result = %s, want inserted", r)
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}

	if err := InTx(ctx, func(tx tx) error {
		r, err := ProcessEvent(ctx, tx, in, "") // redelivery, same payload
		if err != nil {
			return err
		}
		if r != ResultDuplicate {
			t.Fatalf("redelivery result = %s, want duplicate", r)
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}

	n, err := Count(ctx, "events WHERE device_id='dev-t' AND boot_gen=1 AND seq=1")
	if err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("business rows for the dedup key = %d, want exactly 1", n)
	}
	n, _ = Count(ctx, "raw_messages WHERE device_id='dev-t'")
	if n != 2 {
		t.Fatalf("raw audit rows = %d, want 2 (event + dup)", n)
	}
}

// TestRestartDoesNotConfuseSeq: boot 1 seq1 then boot 2 seq1 are different
// business facts; online state follows the newer boot only.
func TestRestartDoesNotConfuseSeq(t *testing.T) {
	ctx := openTestStore(t)
	e1 := mkEvent(1, 1, 1)
	e2 := mkEvent(2, 1, 2)

	for _, e := range []EventInput{e1, e2} {
		if err := InTx(ctx, func(x tx) error { _, err := ProcessEvent(ctx, x, e, ""); return err }); err != nil {
			t.Fatal(err)
		}
	}
	n, _ := Count(ctx, "events WHERE device_id='dev-t'")
	if n != 2 {
		t.Fatalf("events = %d, want 2 (seq1 of each boot are distinct facts)", n)
	}
	max1, _ := MaxEventSeq(ctx, "dev-t", 1)
	max2, _ := MaxEventSeq(ctx, "dev-t", 2)
	if max1 != 1 || max2 != 1 {
		t.Fatalf("max seq boot1=%d boot2=%d, want both 1", max1, max2)
	}

	// Late delivery from the OLD boot must not rewind online state.
	old := mkEvent(1, 99, 9)
	if err := InTx(ctx, func(x tx) error { _, err := ProcessEvent(ctx, x, old, ""); return err }); err != nil {
		t.Fatal(err)
	}
	var boot, seq int64
	err := DB().QueryRow(ctx,
		`SELECT current_boot_gen, current_seq FROM device_state WHERE device_id='dev-t'`).Scan(&boot, &seq)
	if err != nil {
		t.Fatal(err)
	}
	if boot != 2 || seq != 1 {
		t.Fatalf("state rewound by old-boot late packet: boot=%d seq=%d, want boot=2 seq=1", boot, seq)
	}
	// ...but the late fact itself is durably stored, not dropped.
	if n, _ := Count(ctx, "events WHERE device_id='dev-t' AND boot_gen=1 AND seq=99"); n != 1 {
		t.Fatalf("late old-boot fact missing: %d", n)
	}
}

// TestCommitFailureLeavesNothing proves rollback durability semantics: when
// the business step fails, neither the event nor its raw audit row survive.
func TestCommitFailureLeavesNothing(t *testing.T) {
	ctx := openTestStore(t)
	in := mkEvent(1, 7, 1)

	err := InTx(ctx, func(x tx) error {
		_, err := ProcessEvent(ctx, x, in, "dev-t") // force failure
		return err
	})
	if err == nil {
		t.Fatal("expected injected failure, got nil")
	}
	for _, table := range []string{
		"events WHERE device_id='dev-t'",
		"raw_messages WHERE device_id='dev-t'",
		"device_state WHERE device_id='dev-t'",
	} {
		if n, _ := Count(ctx, table); n != 0 {
			t.Fatalf("rolled-back tx left %d rows in %s", n, table)
		}
	}
}

// TestQuarantineAndSnapshot verify the non-event persistence paths commit.
func TestQuarantineAndSnapshot(t *testing.T) {
	ctx := openTestStore(t)
	if err := RecordQuarantine(ctx, "dev-q", "telemetry/dev-q", 3, false, false, []byte("{bad"), "invalid JSON"); err != nil {
		t.Fatal(err)
	}
	if n, _ := Count(ctx, "quarantine"); n != 1 {
		t.Fatalf("quarantine rows = %d, want 1", n)
	}
	if err := RecordSnapshot(ctx, "dev-s", "telemetry/dev-s", 5, []byte(`{"a":1}`)); err != nil {
		t.Fatal(err)
	}
	if n, _ := Count(ctx, "raw_messages WHERE kind='snapshot'"); n != 1 {
		t.Fatalf("snapshot rows = %d, want 1", n)
	}
	if n, _ := Count(ctx, "events"); n != 0 {
		t.Fatalf("quarantine/snapshot created %d events, want 0", n)
	}
}
