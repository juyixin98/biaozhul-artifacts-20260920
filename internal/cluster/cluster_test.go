package cluster_test

import (
	"errors"
	"testing"

	"shardmigrator/internal/cluster"
)

func bootCluster(t *testing.T) *cluster.Cluster {
	t.Helper()
	c := cluster.NewCluster()
	if err := c.AddNode("a"); err != nil {
		t.Fatal(err)
	}
	if err := c.AddNode("b"); err != nil {
		t.Fatal(err)
	}
	if err := c.CreateShard("s1", []string{"a", "b"}); err != nil {
		t.Fatal(err)
	}
	return c
}

func writeOK(t *testing.T, c *cluster.Cluster, payload string) *cluster.Write {
	t.Helper()
	w, replay, err := c.ClientWrite(cluster.WriteInput{ShardID: "s1", Payload: payload, ClientRouteVer: 1})
	if err != nil || replay {
		t.Fatalf("write %q: w=%v replay=%v err=%v", payload, w, replay, err)
	}
	return w
}

// TestSnapshotDeliveryAndCatchup verifies delivery bookkeeping:
// pre-migration writes reach b only via snapshot; writes while b is down in
// CATCHUP reach b only after an explicit catchup flush.
func TestSnapshotDeliveryAndCatchup(t *testing.T) {
	c := bootCluster(t)
	writeOK(t, c, "w1")
	writeOK(t, c, "w2")
	writeOK(t, c, "w3") // start_seq will be 3

	if _, _, err := c.Start("s1", "b", "k-start"); err != nil {
		t.Fatal(err)
	}
	writeOK(t, c, "w4") // during snapshot; not yet on b

	if _, err := c.SetConnected("b", false); err != nil {
		t.Fatal(err)
	}
	writeOK(t, c, "w5") // during snapshot + b down
	if _, _, err := c.CompleteSnapshot("s1", "k-snap"); !errors.Is(err, cluster.ErrTargetDisconnected) {
		t.Fatalf("snapshot with b down err=%v want ErrTargetDisconnected", err)
	}
	if _, err := c.SetConnected("b", true); err != nil {
		t.Fatal(err)
	}
	snap, _, err := c.CompleteSnapshot("s1", "k-snap")
	if err != nil {
		t.Fatal(err)
	}
	if snap.LoadedSeqs != 3 || snap.SnapshotSeq != 3 {
		t.Fatalf("snapshot loaded=%d seq=%d want 3/3", snap.LoadedSeqs, snap.SnapshotSeq)
	}
	// w4 and w5 landed during SNAPSHOT: completing the snapshot must ship them
	// over the incremental channel (regression: they must not open a hole).
	if snap.CaughtSeqs != 2 {
		t.Fatalf("snapshot caught=%d want 2 (snapshot-phase writes)", snap.CaughtSeqs)
	}

	ws, _ := c.Writes("s1")
	if !contains(ws[0].DeliveredTo, "b") || !contains(ws[2].DeliveredTo, "b") {
		t.Errorf("snapshot writes not delivered to b: %v %v", ws[0].DeliveredTo, ws[2].DeliveredTo)
	}
	if !contains(ws[3].DeliveredTo, "b") {
		t.Errorf("w4 written during SNAPSHOT must be delivered on snapshot-complete")
	}
	if !contains(ws[4].DeliveredTo, "b") {
		t.Errorf("w5 written during SNAPSHOT (b down) must be delivered on snapshot-complete")
	}

	// Nothing outstanding immediately after snapshot-complete.
	c1, replay, err := c.Catchup("s1", "k-c1")
	if err != nil {
		t.Fatal(err)
	}
	if c1.Flushed != 0 || !c1.CaughtUp {
		t.Fatalf("catchup1 flushed=%d caughtup=%v want 0/true", c1.Flushed, c1.CaughtUp)
	}
	if replay {
		t.Errorf("first catchup should not be a replay")
	}

	// b goes down in catchup: w6 confirms on a, switch must be blocked.
	if _, err := c.SetConnected("b", false); err != nil {
		t.Fatal(err)
	}
	writeOK(t, c, "w6")
	if _, _, err := c.Switch("s1", "k-sw"); !errors.Is(err, cluster.ErrTargetDisconnected) {
		t.Fatalf("switch with b down err=%v", err)
	}
	if _, err := c.SetConnected("b", true); err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.Switch("s1", "k-sw"); !errors.Is(err, cluster.ErrLagRemaining) {
		t.Fatalf("switch while lagging err=%v want ErrLagRemaining", err)
	}
	if _, _, err := c.Catchup("s1", "k-c2"); err != nil {
		t.Fatal(err)
	}
	sw, _, err := c.Switch("s1", "k-sw")
	if err != nil {
		t.Fatalf("switch after catchup: %v", err)
	}
	if sw.NewPrimaryID != "b" || sw.SwitchSeq != 6 || sw.NewRouteVer != 2 {
		t.Fatalf("bad switch: %+v", sw)
	}
	ws, _ = c.Writes("s1")
	if !contains(ws[5].DeliveredTo, "b") {
		t.Errorf("w6 never delivered to b before switch")
	}
}

// TestPhaseGuards checks out-of-order control messages are rejected.
func TestPhaseGuards(t *testing.T) {
	c := bootCluster(t)
	writeOK(t, c, "w1")

	if _, _, err := c.CompleteSnapshot("s1", ""); !errors.Is(err, cluster.ErrWrongPhase) {
		t.Fatalf("snapshot-complete in RUNNING err=%v", err)
	}
	if _, _, err := c.Catchup("s1", ""); !errors.Is(err, cluster.ErrWrongPhase) {
		t.Fatalf("catchup in RUNNING err=%v", err)
	}
	if _, _, err := c.Switch("s1", ""); !errors.Is(err, cluster.ErrWrongPhase) {
		t.Fatalf("switch in RUNNING err=%v", err)
	}
	if _, _, err := c.Start("s1", "b", ""); err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.Switch("s1", ""); !errors.Is(err, cluster.ErrWrongPhase) {
		t.Fatalf("switch in SNAPSHOT err=%v", err)
	}
}

// TestStaleRouteAndUnknownTarget covers routing checks and target validation.
func TestStaleRouteAndUnknownTarget(t *testing.T) {
	c := bootCluster(t)
	if _, _, err := c.ClientWrite(cluster.WriteInput{ShardID: "s1", Payload: "x", ClientRouteVer: 9}); !errors.Is(err, cluster.ErrStaleRoute) {
		t.Fatalf("stale route err=%v", err)
	}
	if _, _, err := c.Start("s1", "a", ""); !errors.Is(err, cluster.ErrTargetIsPrimary) {
		t.Fatalf("start target=primary err=%v", err)
	}
	if _, _, err := c.Start("s1", "ghost", ""); !errors.Is(err, cluster.ErrBadTarget) {
		t.Fatalf("start unknown target err=%v", err)
	}
	if _, _, err := c.Start("missing", "b", ""); !errors.Is(err, cluster.ErrShardNotFound) {
		t.Fatalf("start missing shard err=%v", err)
	}
}

// TestResetShardWipesState verifies reset rebuilds RUNNING state cleanly.
func TestResetShardWipesState(t *testing.T) {
	c := bootCluster(t)
	writeOK(t, c, "w1")
	if _, _, err := c.Start("s1", "b", ""); err != nil {
		t.Fatal(err)
	}
	v, err := c.ResetShard("s1")
	if err != nil {
		t.Fatal(err)
	}
	if v.Phase != cluster.PhaseRunning || v.PrimaryID != "a" || v.RouteVer != 1 || v.LastSeq != 0 || len(v.Writes) != 0 {
		t.Fatalf("reset left dirty state: %+v", v)
	}
}

func contains(list []string, v string) bool {
	for _, x := range list {
		if x == v {
			return true
		}
	}
	return false
}
