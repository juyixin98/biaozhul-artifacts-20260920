package domain_test

import (
	"strings"
	"testing"

	"github.com/vfxqueue/renderq/internal/domain"
)

func TestValidateRelPath(t *testing.T) {
	good := []string{"a.png", "dir/layer.png", "a/b/c.min.png", "A-B_C1.png"}
	for _, p := range good {
		if err := domain.ValidateRelPath(p); err != nil {
			t.Errorf("expected %q accepted, got %v", p, err)
		}
	}
	bad := map[string]string{
		"":                 "empty",
		"/etc/passwd":      "absolute",
		"../secret.png":    "traversal",
		"a/../../b.png":    "embedded traversal",
		"a/../b.png":       "dotdot segment",
		"./a.png":          "dot segment",
		"a//b.png":         "empty segment",
		"a.png/..":         "trailing dotdot",
		`C:\windows\a.png`: "windows root",
		"a.txt":            "non-png",
		"dir":              "no extension",
	}
	for p, why := range bad {
		if err := domain.ValidateRelPath(p); err == nil {
			t.Errorf("expected %q rejected (%s), got nil", p, why)
		}
	}
}

func TestManifestValidation(t *testing.T) {
	base := func() *domain.Manifest {
		return &domain.Manifest{
			Width: 100, Height: 100,
			Layers: []domain.Layer{
				{ID: "a", AssetPath: "a.png", X: 0, Y: 0},
				{ID: "b", AssetPath: "b.png", X: 10, Y: 10},
			},
		}
	}

	t.Run("ok", func(t *testing.T) {
		m := base()
		if err := m.Validate(); err != nil {
			t.Fatalf("unexpected: %v", err)
		}
	})

	t.Run("duplicate layer id", func(t *testing.T) {
		m := base()
		m.Layers[1].ID = "a"
		if err := m.Validate(); err == nil || !strings.Contains(err.Error(), "duplicate") {
			t.Fatalf("got %v", err)
		}
	})

	t.Run("missing dep", func(t *testing.T) {
		m := base()
		m.Layers[0].Deps = []string{"ghost"}
		if err := m.Validate(); err == nil {
			t.Fatal("expected error")
		}
	})

	t.Run("self dep", func(t *testing.T) {
		m := base()
		m.Layers[0].Deps = []string{"a"}
		if err := m.Validate(); err == nil {
			t.Fatal("expected error")
		}
	})

	t.Run("cycle", func(t *testing.T) {
		m := base()
		m.Layers[0].Deps = []string{"b"}
		m.Layers[1].Deps = []string{"a"}
		err := m.Validate()
		if err == nil || !strings.Contains(err.Error(), "cycle") {
			t.Fatalf("expected cycle error, got %v", err)
		}
	})

	t.Run("cycle of three", func(t *testing.T) {
		m := &domain.Manifest{Width: 10, Height: 10, Layers: []domain.Layer{
			{ID: "a", AssetPath: "a.png", Deps: []string{"c"}},
			{ID: "b", AssetPath: "b.png", Deps: []string{"a"}},
			{ID: "c", AssetPath: "c.png", Deps: []string{"b"}},
		}}
		if err := m.Validate(); err == nil || !strings.Contains(err.Error(), "cycle") {
			t.Fatalf("got %v", err)
		}
	})

	t.Run("opacity range", func(t *testing.T) {
		m := base()
		m.Layers[0].Opacity = 1.5
		if err := m.Validate(); err == nil {
			t.Fatal("expected error")
		}
	})

	t.Run("empty", func(t *testing.T) {
		m := &domain.Manifest{Width: 10, Height: 10}
		if err := m.Validate(); err == nil {
			t.Fatal("expected error")
		}
	})

	t.Run("bad path inside layer", func(t *testing.T) {
		m := base()
		m.Layers[0].AssetPath = "../escape.png"
		if err := m.Validate(); err == nil {
			t.Fatal("expected error")
		}
	})
}

func TestResolveMissingAndBounds(t *testing.T) {
	assets := map[string]struct {
		id, sha string
		w, h    int
	}{
		"a.png": {"id-a", "sha-a", 50, 50},
	}
	fetch := func(p string) (string, string, int, int, error) {
		if a, ok := assets[p]; ok {
			return a.id, a.sha, a.w, a.h, nil
		}
		return "", "", 0, 0, &domain.PathError{Path: p, Msg: "not found"}
	}

	t.Run("missing resource rejected", func(t *testing.T) {
		m := &domain.Manifest{Width: 100, Height: 100, Layers: []domain.Layer{
			{ID: "a", AssetPath: "a.png"},
			{ID: "b", AssetPath: "missing.png"},
		}}
		if _, err := m.Resolve(fetch); err == nil {
			t.Fatal("expected missing asset error")
		}
	})

	t.Run("out of canvas rejected", func(t *testing.T) {
		m := &domain.Manifest{Width: 100, Height: 100, Layers: []domain.Layer{
			{ID: "a", AssetPath: "a.png", X: 60, Y: 0}, // 60+50 > 100
		}}
		_, err := m.Resolve(fetch)
		if err == nil || !strings.Contains(err.Error(), "bounds") {
			t.Fatalf("got %v", err)
		}
	})

	t.Run("negative origin rejected", func(t *testing.T) {
		m := &domain.Manifest{Width: 100, Height: 100, Layers: []domain.Layer{
			{ID: "a", AssetPath: "a.png", X: -1, Y: 0},
		}}
		if _, err := m.Resolve(fetch); err == nil {
			t.Fatal("expected bounds error")
		}
	})

	t.Run("per-frame offset out of bounds", func(t *testing.T) {
		m := &domain.Manifest{Width: 100, Height: 100, Layers: []domain.Layer{
			{ID: "a", AssetPath: "a.png", X: 0, Y: 0, FrameOffsetX: map[int]int{3: 80}},
		}}
		if _, err := m.Resolve(fetch); err == nil {
			t.Fatal("expected bounds error")
		}
	})

	t.Run("resolves and sorts", func(t *testing.T) {
		m := &domain.Manifest{Width: 100, Height: 100, Layers: []domain.Layer{
			{ID: "z", AssetPath: "a.png"},
			{ID: "a", AssetPath: "a.png"},
		}}
		got, err := m.Resolve(fetch)
		if err != nil {
			t.Fatal(err)
		}
		if got[0].LayerID != "a" || got[1].LayerID != "z" {
			t.Fatalf("not sorted: %q, %q", got[0].LayerID, got[1].LayerID)
		}
	})
}
