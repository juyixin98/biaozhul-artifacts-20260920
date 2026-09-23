package logstore

import (
	"path/filepath"
	"testing"
)

func TestMemStoreDeepCopy(t *testing.T) {
	s := NewMemStore()
	st, _ := s.Load()
	st.Log[0].Data = "hacked"
	st.CurrentTerm = 99
	again, _ := s.Load()
	if again.Log[0].Data != "" || again.CurrentTerm != 0 {
		t.Fatalf("Load 返回的不是深拷贝，存储被外部改动: %+v", again)
	}
	st.Log = append(st.Log, Entry{Term: 1, Data: "x"})
	if err := s.Save(st); err != nil {
		t.Fatal(err)
	}
	got, _ := s.Load()
	if len(got.Log) != 2 || got.Log[1].Data != "x" {
		t.Fatalf("Save 后 Load 内容不符: %+v", got.Log)
	}
	// 修改已保存切片不应影响存储。
	st.Log[1].Data = "mutated-after-save"
	got2, _ := s.Load()
	if got2.Log[1].Data != "x" {
		t.Fatalf("Save 没有深拷贝: %q", got2.Log[1].Data)
	}
}

func TestMemStoreWipe(t *testing.T) {
	s := NewMemStore()
	_ = s.Save(State{CurrentTerm: 3, VotedFor: 1, Log: []Entry{{Term: 0}, {Term: 1, Data: "a"}}})
	if err := s.Wipe(); err != nil {
		t.Fatal(err)
	}
	st, _ := s.Load()
	if st.CurrentTerm != 0 || st.VotedFor != -1 || len(st.Log) != 1 {
		t.Fatalf("Wipe 后状态不为空: %+v", st)
	}
}

func TestFileStoreRoundTrip(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "state")
	s, err := NewFileStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	want := State{CurrentTerm: 5, VotedFor: 2, Log: []Entry{
		{Term: 0},
		{Term: 1, Data: "one", ClientID: "c1"},
		{Term: 2, Data: "two", ClientID: "c2"},
	}}
	if err := s.Save(want); err != nil {
		t.Fatal(err)
	}
	// 用新实例重新打开同一目录，验证跨"进程"恢复。
	s2, err := NewFileStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	got, err := s2.Load()
	if err != nil {
		t.Fatal(err)
	}
	if got.CurrentTerm != want.CurrentTerm || got.VotedFor != want.VotedFor || len(got.Log) != 3 {
		t.Fatalf("跨实例恢复内容不符: %+v", got)
	}
	if got.Log[1].Data != "one" || got.Log[2].ClientID != "c2" {
		t.Fatalf("日志条目恢复错误: %+v", got.Log)
	}
}

func TestFileStoreEmptyStartAndWipe(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "fresh")
	s, _ := NewFileStore(dir)
	st, err := s.Load()
	if err != nil {
		t.Fatal(err)
	}
	if st.VotedFor != -1 || len(st.Log) != 1 || st.Log[0].Term != 0 {
		t.Fatalf("全新存储初始状态错误: %+v", st)
	}
	if err := s.Save(State{CurrentTerm: 1, VotedFor: -1, Log: []Entry{{Term: 0}, {Term: 1}}}); err != nil {
		t.Fatal(err)
	}
	if err := s.Wipe(); err != nil {
		t.Fatal(err)
	}
	st2, _ := s.Load()
	if st2.CurrentTerm != 0 || len(st2.Log) != 1 {
		t.Fatalf("Wipe 后重新加载错误: %+v", st2)
	}
}
