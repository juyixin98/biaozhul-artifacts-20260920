package compositor

import (
	"encoding/json"
	"strings"
	"testing"
)

func alwaysExists(string) bool { return true }

func TestValidate_MissingResource(t *testing.T) {
	s := &Spec{CanvasWidth: 8, CanvasHeight: 8, FrameCount: 1,
		Layers: []Layer{{ID: "a", AssetID: "x"}}}
	err := s.Validate(func(string) bool { return false })
	if err == nil || !strings.Contains(err.Error(), "missing resource") {
		t.Fatalf("expected missing resource error, got %v", err)
	}
}

func TestValidate_UnknownDependency(t *testing.T) {
	s := &Spec{CanvasWidth: 8, CanvasHeight: 8, FrameCount: 1,
		Layers: []Layer{{ID: "a", AssetID: "x", Deps: []string{"ghost"}}}}
	if err := s.Validate(alwaysExists); err == nil ||
		!strings.Contains(err.Error(), "unknown layer") {
		t.Fatalf("expected unknown dependency error, got %v", err)
	}
}

func TestValidate_DirectCycle(t *testing.T) {
	s := &Spec{CanvasWidth: 8, CanvasHeight: 8, FrameCount: 1,
		Layers: []Layer{
			{ID: "a", AssetID: "x", Deps: []string{"b"}},
			{ID: "b", AssetID: "y", Deps: []string{"a"}},
		}}
	if err := s.Validate(alwaysExists); err == nil ||
		!strings.Contains(err.Error(), "cycle") {
		t.Fatalf("expected cycle error, got %v", err)
	}
}

func TestValidate_ThreeNodeCycle(t *testing.T) {
	s := &Spec{CanvasWidth: 8, CanvasHeight: 8, FrameCount: 1,
		Layers: []Layer{
			{ID: "a", AssetID: "x", Deps: []string{"c"}},
			{ID: "b", AssetID: "y", Deps: []string{"a"}},
			{ID: "c", AssetID: "z", Deps: []string{"b"}},
		}}
	if err := s.Validate(alwaysExists); err == nil {
		t.Fatal("expected cycle error")
	}
}

func TestValidate_DuplicateLayer(t *testing.T) {
	s := &Spec{CanvasWidth: 8, CanvasHeight: 8, FrameCount: 1,
		Layers: []Layer{
			{ID: "a", AssetID: "x"},
			{ID: "a", AssetID: "y"},
		}}
	if err := s.Validate(alwaysExists); err == nil {
		t.Fatal("expected duplicate layer error")
	}
}

func TestValidate_OutOfBoundsOrigin(t *testing.T) {
	s := &Spec{CanvasWidth: 8, CanvasHeight: 8, FrameCount: 1,
		Layers: []Layer{{ID: "a", AssetID: "x", X: 9, Y: 0}}}
	if err := s.Validate(alwaysExists); err == nil {
		t.Fatal("expected out-of-bounds error")
	}
}

func TestDrawOrder_RespectsDeps(t *testing.T) {
	s := &Spec{CanvasWidth: 8, CanvasHeight: 8, FrameCount: 1,
		Layers: []Layer{
			{ID: "top", AssetID: "1", Deps: []string{"bot"}},
			{ID: "bot", AssetID: "2"},
		}}
	if err := s.Validate(alwaysExists); err != nil {
		t.Fatal(err)
	}
	order, err := s.DrawOrder()
	if err != nil {
		t.Fatal(err)
	}
	if s.Layers[order[0]].ID != "bot" || s.Layers[order[1]].ID != "top" {
		t.Fatalf("draw order = %v, want bot before top", order)
	}
}

func TestValidate_BadFrameWindow(t *testing.T) {
	s := &Spec{CanvasWidth: 8, CanvasHeight: 8, FrameCount: 3,
		Layers: []Layer{{ID: "a", AssetID: "x", FrameStart: 2, FrameEnd: 5}}}
	if err := s.Validate(alwaysExists); err == nil {
		t.Fatal("expected frame window error")
	}
}

func TestCanonicalJSON_Stable(t *testing.T) {
	s := &Spec{CanvasWidth: 8, CanvasHeight: 8, FrameCount: 1,
		Layers: []Layer{{ID: "a", AssetID: "x"}}}
	b1, err := CanonicalJSON(s)
	if err != nil {
		t.Fatal(err)
	}
	var roundtrip Spec
	if err := json.Unmarshal(b1, &roundtrip); err != nil {
		t.Fatal(err)
	}
	b2, err := CanonicalJSON(&roundtrip)
	if err != nil {
		t.Fatal(err)
	}
	if string(b1) != string(b2) {
		t.Fatalf("canonical json not stable:\n%s\n%s", b1, b2)
	}
}
