package delta

import (
	"bytes"
	"testing"
)

// FuzzGenerateRoundTrip feeds arbitrary old/new byte pairs and asserts the
// generated patch reconstructs new exactly and binds correct digests.
//
// Run: go test ./internal/delta -fuzz=FuzzGenerateRoundTrip -fuzztime=30s
func FuzzGenerateRoundTrip(f *testing.F) {
	f.Add([]byte("hello world"), []byte("hello brave new world"), 16)
	f.Add(make([]byte, 4096), make([]byte, 1), 64)
	f.Add([]byte("aaaaaaa"), []byte("aaaabaaa"), 4)
	f.Add([]byte{}, []byte("created"), 32)
	f.Add([]byte("deleted"), []byte{}, 32)

	f.Fuzz(func(t *testing.T, old, new []byte, bs int) {
		// Keep block sizes in range and avoid zero.
		if bs < MinBlockSize {
			bs = MinBlockSize
		}
		if bs > 4096 {
			bs = 4096
		}
		// Bound total work for the fuzzer.
		if len(old) > 200000 || len(new) > 200000 {
			return
		}
		p, err := Generate(bytes.NewReader(old), int64(len(old)),
			bytes.NewReader(new), int64(len(new)), bs)
		if err != nil {
			t.Fatalf("Generate: %v", err)
		}
		if p.OldSum != DigestBytes(old) || p.NewSum != DigestBytes(new) {
			t.Fatal("bound digests wrong")
		}
		if err := p.VerifyPatchSum(); err != nil {
			t.Fatalf("patchSum: %v", err)
		}
		var out bytes.Buffer
		for _, op := range p.Ops {
			switch op.Type {
			case OpData:
				out.Write(op.Data)
			case OpCopy:
				start := int64(op.OldIndex) * int64(bs)
				end := start + int64(bs)
				if end > int64(len(old)) {
					end = int64(len(old))
				}
				out.Write(old[start:end])
			}
		}
		if !bytes.Equal(out.Bytes(), new) {
			t.Fatalf("round trip mismatch: got %d bytes want %d", out.Len(), len(new))
		}
	})
}
