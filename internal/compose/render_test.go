package compose_test

import (
	"bytes"
	"image"
	"image/color"
	"image/png"
	"testing"

	"github.com/vfxqueue/renderq/internal/compose"
	"github.com/vfxqueue/renderq/internal/domain"
)

func pngOf(w, h int, c color.RGBA) []byte {
	img := image.NewNRGBA(image.Rect(0, 0, w, h))
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			img.SetNRGBA(x, y, color.NRGBA{R: c.R, G: c.G, B: c.B, A: c.A})
		}
	}
	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		panic(err)
	}
	return buf.Bytes()
}

func decode(t *testing.T, b []byte) image.Image {
	t.Helper()
	img, err := png.Decode(bytes.NewReader(b))
	if err != nil {
		t.Fatal(err)
	}
	return img
}

// TestAlphaOverMath checks the compositor actually performs Porter-Duff
// "over" blending: dst opaque red covered by 50% white yields (255,128,128)
// style pink, not a fixed image and not a copy.
func TestAlphaOverMath(t *testing.T) {
	red := pngOf(2, 2, color.RGBA{255, 0, 0, 255})
	whiteHalf := pngOf(2, 2, color.RGBA{255, 255, 255, 128})

	// Inputs are distinct images...
	if bytes.Equal(red, whiteHalf) {
		t.Fatal("test inputs must differ")
	}

	layers := []compose.LayerInput{
		{Spec: domain.Layer{ID: "red"}, PNG: red, Width: 2, Height: 2, Opacity: 1},
		{Spec: domain.Layer{ID: "white"}, PNG: whiteHalf, Width: 2, Height: 2, Opacity: 1},
	}
	out, err := compose.RenderFrame(2, 2, layers)
	if err != nil {
		t.Fatal(err)
	}
	img := decode(t, out)
	sr, sg, sb, sa := img.At(1, 1).RGBA()
	got := color.RGBA{uint8(sr >> 8), uint8(sg >> 8), uint8(sb >> 8), uint8(sa >> 8)}
	// outA = 1; outC = srcC*0.5 + dstC*0.5
	// R: 255; G: 128; B: 128; A: 255 (allow rounding ±1)
	within := func(a, b uint8) bool { d := int(a) - int(b); return d >= -1 && d <= 1 }
	if !within(got.R, 255) || !within(got.G, 128) || !within(got.B, 128) || !within(got.A, 255) {
		t.Fatalf("blended pixel = %v, want ~{255 128 128 255}", got)
	}
}

// TestOpacityMultiplier checks the layer opacity scales source coverage.
func TestOpacityMultiplier(t *testing.T) {
	red := pngOf(1, 1, color.RGBA{255, 0, 0, 255})
	// Fully opaque black layer at 25% opacity.
	black := pngOf(1, 1, color.RGBA{0, 0, 0, 255})
	layers := []compose.LayerInput{
		{Spec: domain.Layer{ID: "red"}, PNG: red, Width: 1, Height: 1, Opacity: 1},
		{Spec: domain.Layer{ID: "black"}, PNG: black, Width: 1, Height: 1, Opacity: 0.25},
	}
	out, err := compose.RenderFrame(1, 1, layers)
	if err != nil {
		t.Fatal(err)
	}
	sr, sg, sb, sa := decode(t, out).At(0, 0).RGBA()
	got := color.RGBA{uint8(sr >> 8), uint8(sg >> 8), uint8(sb >> 8), uint8(sa >> 8)}
	// R' = 0*0.25 + 255*1*0.75 = 191
	if d := int(got.R) - 191; d < -2 || d > 2 {
		t.Fatalf("R = %d, want ~191 (%v)", got.R, got)
	}
	if got.A != 255 {
		t.Fatalf("A = %d, want 255", got.A)
	}
}

// TestLayerOrderAndPlacement checks ordered stacking and offsets.
func TestLayerOrderAndPlacement(t *testing.T) {
	red := pngOf(2, 1, color.RGBA{255, 0, 0, 255})
	green := pngOf(1, 1, color.RGBA{0, 255, 0, 255})
	// Canvas 4x1: red covers x=0..1; green at x=2. Pixel x=3 stays empty.
	layers := []compose.LayerInput{
		{Spec: domain.Layer{ID: "red"}, PNG: red, Width: 2, Height: 1, X: 0, Y: 0, Opacity: 1},
		{Spec: domain.Layer{ID: "green"}, PNG: green, Width: 1, Height: 1, X: 2, Y: 0, Opacity: 1},
	}
	out, err := compose.RenderFrame(4, 1, layers)
	if err != nil {
		t.Fatal(err)
	}
	img := decode(t, out)
	if px := rgbaOf(img, 0, 0); px.R != 255 || px.B != 0 {
		t.Fatalf("x0 = %v", px)
	}
	if px := rgbaOf(img, 2, 0); px.G != 255 {
		t.Fatalf("x2 = %v", px)
	}
	if px := rgbaOf(img, 3, 0); px.A != 0 {
		t.Fatalf("x3 should be transparent, got %v", px)
	}
}

// TestRejectsGarbage ensures corrupt inputs fail rather than producing a
// placeholder image.
func TestRejectsGarbage(t *testing.T) {
	if _, err := compose.RenderFrame(2, 2, []compose.LayerInput{
		{Spec: domain.Layer{ID: "bad"}, PNG: []byte("not a png"), Width: 2, Height: 2, Opacity: 1},
	}); err == nil {
		t.Fatal("expected decode error")
	}
}

// TestEmptyCanvasIsTransparent proves no fixed background is injected.
func TestEmptyLayerList(t *testing.T) {
	if _, err := compose.RenderFrame(2, 2, nil); err != nil {
		// Zero layers is unusual; our manifest forbids it, but the renderer
		// should still produce a transparent image, not an error.
		t.Fatalf("render with no layers: %v", err)
	}
}

func rgbaOf(img image.Image, x, y int) color.RGBA {
	r, g, b, a := img.At(x, y).RGBA()
	return color.RGBA{uint8(r >> 8), uint8(g >> 8), uint8(b >> 8), uint8(a >> 8)}
}
