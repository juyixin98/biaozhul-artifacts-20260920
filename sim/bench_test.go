package sim

import "testing"

func BenchmarkExecuteNested(b *testing.B) {
	wl, _ := Preset("nested")
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		if _, err := Execute(wl, true); err != nil {
			b.Fatal(err)
		}
	}
}

func BenchmarkCompareNested(b *testing.B) {
	wl, _ := Preset("nested")
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		if _, err := Compare(wl); err != nil {
			b.Fatal(err)
		}
	}
}
