package compositor

import (
	"bytes"
	"image"
	"image/color"
	"image/png"
	"os"
	"path/filepath"
	"testing"
)

// makePNG writes a solid, possibly semi-transparent PNG and returns its path.
func makePNG(t *testing.T, dir, name string, w, h int, c color.NRGBA) string {
	t.Helper()
	img := image.NewNRGBA(image.Rect(0, 0, w, h))
	for i := range img.Pix {
		img.Pix[i] = 0
	}
	b := img.Bounds()
	for y := b.Min.Y; y < b.Max.Y; y++ {
		for x := b.Min.X; x < b.Max.X; x++ {
			img.SetNRGBA(x, y, c)
		}
	}
	path := filepath.Join(dir, name)
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if err := png.Encode(f, img); err != nil {
		t.Fatal(err)
	}
	return path
}

func decodeFrame(t *testing.T, data []byte) *image.NRGBA {
	t.Helper()
	im, err := png.Decode(bytes.NewReader(data))
	if err != nil {
		t.Fatal(err)
	}
	out := image.NewNRGBA(im.Bounds())
	b := im.Bounds()
	for y := b.Min.Y; y < b.Max.Y; y++ {
		for x := b.Min.X; x < b.Max.X; x++ {
			out.Set(x, y, im.At(x, y))
		}
	}
	return out
}

// TestRenderFrame_SourceOver proves output pixels are really computed from the
// inputs: a half-transparent red layer over an opaque blue canvas yields the
// expected source-over blend (not a placeholder, not an empty file).
func TestRenderFrame_SourceOver(t *testing.T) {
	dir := t.TempDir()
	bgPath := makePNG(t, dir, "bg.png", 4, 4, color.NRGBA{0, 0, 255, 255})
	fgPath := makePNG(t, dir, "fg.png", 4, 4, color.NRGBA{255, 0, 0, 128})

	spec := &Spec{CanvasWidth: 4, CanvasHeight: 4, FrameCount: 1,
		Layers: []Layer{
			{ID: "bg", AssetID: "bg"},
			{ID: "fg", AssetID: "fg", Deps: []string{"bg"}},
		}}
	order, err := spec.DrawOrder()
	if err != nil {
		t.Fatal(err)
	}
	res := map[string]Resource{
		"bg": {LayerID: "bg", AssetPath: bgPath},
		"fg": {LayerID: "fg", AssetPath: fgPath},
	}
	data, sum, err := RenderFrame(spec, order, res, 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(data) == 0 {
		t.Fatal("empty output")
	}
	if len(sum) != 64 {
		t.Fatalf("bad sha256 %q", sum)
	}
	img := decodeFrame(t, data)
	got := img.NRGBAAt(0, 0)
	// Expected straight-alpha source-over: fg (255,0,0,a=128) over bg blue.
	// a' = 128 + 255*(127/255) = 255
	// r' = 255*128/255 = 128
	// b' = 255*(127/255) = 127
	if got.A != 255 {
		t.Fatalf("alpha = %d, want 255", got.A)
	}
	if !closeTo(int(got.R), 128, 2) || !closeTo(int(got.B), 127, 2) || got.G != 0 {
		t.Fatalf("blended pixel = %+v, want ~{128,0,127,255}", got)
	}
}

func TestRenderFrame_DependencyOrdering(t *testing.T) {
	dir := t.TempDir()
	// Two fully opaque 2x2 layers at the same origin: whichever is drawn last
	// wins. Declare "second" as depending on "first".
	first := makePNG(t, dir, "first.png", 2, 2, color.NRGBA{10, 20, 30, 255})
	second := makePNG(t, dir, "second.png", 2, 2, color.NRGBA{200, 100, 50, 255})
	spec := &Spec{CanvasWidth: 2, CanvasHeight: 2, FrameCount: 1,
		Layers: []Layer{
			{ID: "second", AssetID: "second", Deps: []string{"first"}},
			{ID: "first", AssetID: "first"},
		}}
	order, err := spec.DrawOrder()
	if err != nil {
		t.Fatal(err)
	}
	res := map[string]Resource{
		"first":  {AssetPath: first},
		"second": {AssetPath: second},
	}
	data, _, err := RenderFrame(spec, order, res, 0)
	if err != nil {
		t.Fatal(err)
	}
	img := decodeFrame(t, data)
	got := img.NRGBAAt(0, 0)
	if got.R != 200 || got.G != 100 || got.B != 50 {
		t.Fatalf("pixel = %+v, want second layer on top (200,100,50)", got)
	}
}

func TestRenderFrame_MissingFileFails(t *testing.T) {
	spec := &Spec{CanvasWidth: 2, CanvasHeight: 2, FrameCount: 1,
		Layers: []Layer{{ID: "a", AssetID: "a"}}}
	order, _ := spec.DrawOrder()
	res := map[string]Resource{"a": {AssetPath: filepath.Join(t.TempDir(), "nope.png")}}
	if _, _, err := RenderFrame(spec, order, res, 0); err == nil {
		t.Fatal("expected decode error for missing file")
	}
}

func TestRenderFrame_DifferentInputsDifferentDigest(t *testing.T) {
	dir := t.TempDir()
	a := makePNG(t, dir, "a.png", 4, 4, color.NRGBA{1, 2, 3, 255})
	b := makePNG(t, dir, "b.png", 4, 4, color.NRGBA{4, 5, 6, 255})
	spec := &Spec{CanvasWidth: 4, CanvasHeight: 4, FrameCount: 1,
		Layers: []Layer{{ID: "l", AssetID: "l"}}}
	order, _ := spec.DrawOrder()
	d1, h1, err := RenderFrame(spec, order, map[string]Resource{"l": {AssetPath: a}}, 0)
	if err != nil {
		t.Fatal(err)
	}
	d2, h2, err := RenderFrame(spec, order, map[string]Resource{"l": {AssetPath: b}}, 0)
	if err != nil {
		t.Fatal(err)
	}
	if h1 == h2 || bytes.Equal(d1, d2) {
		t.Fatal("different inputs produced identical output")
	}
}

func TestLayerVisible(t *testing.T) {
	l := Layer{} // zero window => always visible
	if !l.Visible(0) || !l.Visible(99) {
		t.Fatal("zero-value window should be fully visible")
	}
	l2 := Layer{FrameStart: 2, FrameEnd: 4}
	for f, want := range map[int]bool{1: false, 2: true, 4: true, 5: false} {
		if l2.Visible(f) != want {
			t.Fatalf("frame %d visible = %v want %v", f, !want, want)
		}
	}
}

func closeTo(a, b, tol int) bool {
	d := a - b
	if d < 0 {
		d = -d
	}
	return d <= tol
}
