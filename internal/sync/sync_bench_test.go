package sync_test

import (
	"context"
	"fmt"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/example/merklekv/internal/hlc"
	"github.com/example/merklekv/internal/merkle"
	"github.com/example/merklekv/internal/server"
	"github.com/example/merklekv/internal/store"
	synclib "github.com/example/merklekv/internal/sync"
)

// BenchmarkSingleLeafSync shows that the steady-state cost of an
// anti-entropy sweep over a converged pair is independent of dataset size:
// each run only creates a snapshot on each side and compares two root
// hashes. The first iteration converges the single-key change; later
// iterations measure the steady-state "roots equal" cost.
func BenchmarkSingleLeafSync(b *testing.B) {
	for _, n := range []int{1000, 4000, 16000} {
		b.Run(fmt.Sprintf("keys=%d", n), func(b *testing.B) {
			params, _ := merkle.NewParams(16, 3)
			mk := func(name string) (*store.Store, *httptest.Server) {
				st := store.New(store.Config{ReplicaID: name})
				srv := httptest.NewServer(server.New(st, params, nil).Handler())
				b.Cleanup(srv.Close)
				return st, srv
			}
			s1, srv1 := mk("r1")
			s2, srv2 := mk("r2")

			base := records(n, 256)
			entries := toEntries("base", base)
			s1.Seed(entries)

			changed := make([]store.Entry, len(entries))
			copy(changed, entries)
			changed[7] = store.Entry{
				Key: changed[7].Key, Value: []byte(strings.Repeat("Z", 256)),
				Ver: hlc.Timestamp{Wall: 9_000_000}, Origin: "r2",
			}
			s2.Seed(changed)

			client, err := synclib.NewClient(synclib.Config{
				Local: s1, Params: params, Peer: srv2.URL, MaxRounds: 3,
			})
			if err != nil {
				b.Fatal(err)
			}
			b.ReportAllocs()
			b.ResetTimer()
			var last *synclib.Result
			for i := 0; i < b.N; i++ {
				last, err = client.Run(context.Background())
				if err != nil {
					b.Fatal(err)
				}
			}
			b.StopTimer()
			_ = srv1
			b.ReportMetric(float64(last.HashNodesExchanged), "nodes/run")
			b.ReportMetric(float64(last.EntriesExchanged), "entries/run")
			b.ReportMetric(float64(last.WireBytesExchanged), "wireB/run")
		})
	}
}
