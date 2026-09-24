package cluster_test

import (
	"errors"
	"testing"

	"shardmigrator/internal/cluster"
)

// TestSnapshotPhaseWritesNoHole is a regression test for a delivery hole:
// writes that confirm DURING the snapshot phase are outside the bulk snapshot
// (seq > start_seq); a later CATCHUP write streamed to the target must not let
// the recorded watermark skip the earlier snapshot-phase write. After the
// switch the new primary must hold every confirmed write.
func TestSnapshotPhaseWritesNoHole(t *testing.T) {
	c := bootCluster(t)

	writeOK(t, c, "base-1") // seq1 -> start_seq
	if _, _, err := c.Start("s1", "b", "start"); err != nil {
		t.Fatal(err)
	}
	writeOK(t, c, "during-snapshot-1") // seq2 (SNAPSHOT)
	writeOK(t, c, "during-snapshot-2") // seq3 (SNAPSHOT)

	if _, _, err := c.CompleteSnapshot("s1", "snap"); err != nil {
		t.Fatal(err)
	}
	writeOK(t, c, "catchup-streamed") // seq4 (CATCHUP, streamed live, pushes watermark)

	// No explicit catchup call before switch: snapshot-complete must already
	// have delivered seq2/seq3, so the switch must succeed at zero loss.
	sw, _, err := c.Switch("s1", "sw")
	if err != nil {
		t.Fatalf("switch blocked despite complete delivery: %v", err)
	}
	if sw.SwitchSeq != 4 {
		t.Fatalf("switch_seq=%d want 4", sw.SwitchSeq)
	}

	ws, _ := c.Writes("s1")
	for i, w := range ws {
		if !contains(w.DeliveredTo, "b") {
			t.Errorf("seq %d (%q) missing on new primary b: delivered=%v",
				w.Seq, w.Payload, w.DeliveredTo)
		}
		if i+1 != w.Seq {
			t.Errorf("non-dense seq at index %d: %d", i, w.Seq)
		}
	}

	aud, err := c.Audit("s1")
	if err != nil {
		t.Fatal(err)
	}
	if !aud.Healthy {
		t.Fatalf("audit unhealthy: %+v", aud)
	}
}

// TestAuditRequiresFullDelivery checks that the audit flags a target which is
// missing confirmed writes at the moment of the (forced-view) switched state.
func TestAuditRequiresFullDelivery(t *testing.T) {
	c := bootCluster(t)
	writeOK(t, c, "w1")
	if _, _, err := c.Start("s1", "b", ""); err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.CompleteSnapshot("s1", ""); err != nil {
		t.Fatal(err)
	}
	// Disconnect target during CATCHUP; the write is confirmed only on a.
	if _, err := c.SetConnected("b", false); err != nil {
		t.Fatal(err)
	}
	writeOK(t, c, "w-missed")
	if _, _, err := c.Switch("s1", ""); !errors.Is(err, cluster.ErrTargetDisconnected) {
		t.Fatalf("switch with down target err=%v", err)
	}
	// Reconnect but do NOT catch up: switch must still be refused (lag).
	if _, err := c.SetConnected("b", true); err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.Switch("s1", ""); !errors.Is(err, cluster.ErrLagRemaining) {
		t.Fatalf("switch with missing delivery err=%v want lag", err)
	}
	if _, _, err := c.Catchup("s1", ""); err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.Switch("s1", ""); err != nil {
		t.Fatalf("switch after catchup: %v", err)
	}
	aud, _ := c.Audit("s1")
	if aud.TargetMissing != 0 || !aud.Healthy {
		t.Fatalf("audit after full catchup: %+v", aud)
	}
}
