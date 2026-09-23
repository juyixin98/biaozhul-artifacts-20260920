package store

import (
	"sync"
	"testing"

	"criticalpath/internal/analyzer"
)

func TestConcurrentUpsert(t *testing.T) {
	st, err := Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	var wg sync.WaitGroup
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for i := 0; i < 50; i++ {
				_ = st.Upsert(analyzer.Span{
					TraceID: "conc", SpanID: "s", Name: "upserted",
					StartUs: int64(i), EndUs: int64(i + 1),
				})
			}
		}(g)
	}
	wg.Wait()
	got, ok := st.GetTrace("conc")
	if !ok || len(got) != 1 {
		t.Fatalf("并发 upsert 后应只有 1 个 span，实际 %d (%v)", len(got), ok)
	}
}
