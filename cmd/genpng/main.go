// Command genpng generates sample semi-transparent PNG layers and a
// reference manifest into ./samples, so the service can be exercised
// end-to-end without external assets. Every file is a real, independently
// rendered PNG (a colored rounded block over transparency).
package main

import (
	"image"
	"image/color"
	"image/png"
	"os"
	"path/filepath"
)

type spec struct {
	name string
	w, h int
	rgba color.RGBA
}

func main() {
	dir := "samples"
	if len(os.Args) > 1 {
		dir = os.Args[1]
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		panic(err)
	}

	specs := []spec{
		{"bg.png", 120, 80, color.RGBA{30, 60, 120, 255}},
		{"mid.png", 80, 50, color.RGBA{220, 180, 40, 180}},
		{"top.png", 40, 30, color.RGBA{220, 60, 60, 120}},
		{"edge.png", 20, 20, color.RGBA{40, 200, 120, 200}},
	}
	for _, s := range specs {
		if err := writeBlock(filepath.Join(dir, s.name), s.w, s.h, s.rgba); err != nil {
			panic(err)
		}
	}
}

// writeBlock paints an opaque/alpha colored rectangle with a one-pixel
// fully-transparent border, exercising the alpha path in the compositor.
func writeBlock(path string, w, h int, c color.RGBA) error {
	img := image.NewNRGBA(image.Rect(0, 0, w, h))
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			if x == 0 || y == 0 || x == w-1 || y == h-1 {
				img.SetNRGBA(x, y, color.NRGBA{0, 0, 0, 0})
				continue
			}
			img.SetNRGBA(x, y, color.NRGBA{R: c.R, G: c.G, B: c.B, A: c.A})
		}
	}
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	return png.Encode(f, img)
}
