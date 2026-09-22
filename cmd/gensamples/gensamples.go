// Command gensamples writes the sample PNG layers used by the docs and tests
// into ./samples (or the first argument's directory).
package main

import (
	"image"
	"image/color"
	"image/png"
	"log"
	"os"
	"path/filepath"
)

func main() {
	dir := "samples"
	if len(os.Args) > 1 {
		dir = os.Args[1]
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		log.Fatal(err)
	}

	writePNG(filepath.Join(dir, "bg.png"), 320, 240, func(img *image.NRGBA) {
		b := img.Bounds()
		for y := b.Min.Y; y < b.Max.Y; y++ {
			for x := b.Min.X; x < b.Max.X; x++ {
				img.SetNRGBA(x, y, color.NRGBA{
					R: uint8(20 + x%60), G: uint8(40 + y%60), B: 120, A: 255,
				})
			}
		}
	})
	writePNG(filepath.Join(dir, "circle.png"), 120, 120, func(img *image.NRGBA) {
		cx, cy := 60, 60
		b := img.Bounds()
		for y := b.Min.Y; y < b.Max.Y; y++ {
			for x := b.Min.X; x < b.Max.X; x++ {
				dx, dy := x-cx, y-cy
				d2 := dx*dx + dy*dy
				switch {
				case d2 <= 45*45:
					img.SetNRGBA(x, y, color.NRGBA{R: 230, G: 120, B: 40, A: 255})
				case d2 <= 55*55:
					img.SetNRGBA(x, y, color.NRGBA{R: 230, G: 120, B: 40, A: 90})
				}
			}
		}
	})
	writePNG(filepath.Join(dir, "badge.png"), 80, 80, func(img *image.NRGBA) {
		b := img.Bounds()
		for y := b.Min.Y; y < b.Max.Y; y++ {
			for x := b.Min.X; x < b.Max.X; x++ {
				if (x/10+y/10)%2 == 0 {
					img.SetNRGBA(x, y, color.NRGBA{R: 255, G: 230, B: 80, A: 230})
				}
			}
		}
	})
	log.Printf("wrote bg.png, circle.png, badge.png to %s", dir)
}

func writePNG(path string, w, h int, draw func(*image.NRGBA)) {
	img := image.NewNRGBA(image.Rect(0, 0, w, h))
	draw(img)
	f, err := os.Create(path)
	if err != nil {
		log.Fatal(err)
	}
	defer f.Close()
	if err := png.Encode(f, img); err != nil {
		log.Fatal(err)
	}
}
