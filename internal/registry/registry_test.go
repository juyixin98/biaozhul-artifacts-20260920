package registry

import "testing"

func TestPutGetList(t *testing.T) {
	s := New()
	c1 := Contract{Name: "user-api", Version: "v1", Schema: map[string]any{"type": "object"}}
	c2 := Contract{Name: "user-api", Version: "v2", Schema: map[string]any{"type": "object"}}
	s.Put(c1)
	s.Put(c2)

	got, err := s.Get("user-api", "v2")
	if err != nil {
		t.Fatal(err)
	}
	if got.Name != "user-api" || got.Version != "v2" {
		t.Fatalf("取回契约错误: %+v", got)
	}

	if _, err := s.Get("user-api", "v9"); err == nil {
		t.Fatal("不存在的版本应报错")
	}
	if _, err := s.Get("nope", "v1"); err == nil {
		t.Fatal("不存在的契约应报错")
	}

	list := s.List()
	if len(list) != 2 || list[0].Version != "v1" || list[1].Version != "v2" {
		t.Fatalf("列表应有序: %+v", list)
	}
}

func TestPutOverwrite(t *testing.T) {
	s := New()
	s.Put(Contract{Name: "a", Version: "v1", Schema: map[string]any{"type": "string"}})
	s.Put(Contract{Name: "a", Version: "v1", Schema: map[string]any{"type": "integer"}})
	got, _ := s.Get("a", "v1")
	if got.Schema["type"] != "integer" {
		t.Fatalf("重复 Put 应覆盖: %+v", got)
	}
	if len(s.List()) != 1 {
		t.Fatal("覆盖不应增加条目")
	}
}
