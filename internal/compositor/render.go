package compositor

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"image"
	"image/png"
	"os"
)

// Resource is one frozen layer asset loaded for a render.
type Resource struct {
	LayerID   string
	AssetPath string
	SHA256    string
	Width     int
	Height    int
}

// RenderFrame reads every visible layer's real PNG file from disk and
// composites it over the canvas with source-over alpha blending in dependency
// (draw) order. Returns the encoded PNG and its SHA-256. No placeholder or
// fixed images are used: every output pixel derives from the inputs.
func RenderFrame(spec *Spec, order []int, res map[string]Resource, frame int) ([]byte, string, error) {
	canvas := image.NewNRGBA(image.Rect(0, 0, spec.CanvasWidth, spec.CanvasHeight))
	// NRGBA zero value is fully transparent black, which is the correct start.

	for _, li := range order {
		layer := spec.Layers[li]
		if !layer.Visible(frame) {
			continue
		}
		resrc, ok := res[layer.ID]
		if !ok {
			return nil, "", fmt.Errorf("frame %d: layer %q has no frozen resource", frame, layer.ID)
		}
		img, err := decodePNG(resrc.AssetPath)
		if err != nil {
			return nil, "", fmt.Errorf("frame %d: layer %q: decode %s: %w", frame, layer.ID, resrc.AssetPath, err)
		}
		blit(canvas, img, layer.X, layer.Y, spec.CanvasWidth, spec.CanvasHeight)
	}

	var buf bytes.Buffer
	if err := png.Encode(&buf, canvas); err != nil {
		return nil, "", err
	}
	sum := sha256.Sum256(buf.Bytes())
	return buf.Bytes(), hex.EncodeToString(sum[:]), nil
}

// decodePNG actually reads and decodes the file from disk.
func decodePNG(path string) (image.Image, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	img, err := png.Decode(f)
	if err != nil {
		return nil, err
	}
	return img, nil
}

// blit draws src onto dst at (ox,oy) with source-over blending, clipped to the
// canvas rectangle.
func blit(dst *image.NRGBA, src image.Image, ox, oy, cw, ch int) {
	b := src.Bounds()
	for y := b.Min.Y; y < b.Max.Y; y++ {
		dy := oy + (y - b.Min.Y)
		if dy < 0 || dy >= ch {
			continue
		}
		for x := b.Min.X; x < b.Max.X; x++ {
			dx := ox + (x - b.Min.X)
			if dx < 0 || dx >= cw {
				continue
			}
			r, g, bl, a := src.At(x, y).RGBA()
			if a == 0 {
				continue
			}
			i := dst.PixOffset(dx, dy)
			// src.At() returns 16-bit premultiplied values.
			sa := uint32(a >> 8) // 0..255 source alpha
			if sa == 255 {
				dst.Pix[i+0] = uint8(r >> 8)
				dst.Pix[i+1] = uint8(g >> 8)
				dst.Pix[i+2] = uint8(bl >> 8)
				dst.Pix[i+3] = 255
				continue
			}
			// Un-premultiply source to straight alpha.
			sr := int(unpremultiply(uint8(r>>8), sa))
			sg := int(unpremultiply(uint8(g>>8), sa))
			sb := int(unpremultiply(uint8(bl>>8), sa))
			saI := int(sa)
			da := int(dst.Pix[i+3])
			inv := 255 - saI
			// straight-alpha source-over
			outA := saI + da*inv/255
			if outA == 0 {
				continue
			}
			dst.Pix[i+0] = uint8((sr*saI + int(dst.Pix[i+0])*da*inv/255) / outA)
			dst.Pix[i+1] = uint8((sg*saI + int(dst.Pix[i+1])*da*inv/255) / outA)
			dst.Pix[i+2] = uint8((sb*saI + int(dst.Pix[i+2])*da*inv/255) / outA)
			dst.Pix[i+3] = uint8(outA)
		}
	}
}

// unpremultiply converts an 8-bit premultiplied channel back to straight.
func unpremultiply(c uint8, a uint32) uint32 {
	if a == 0 {
		return 0
	}
	v := uint32(c) * 255
	return (v + a/2) / a
}

// VerifyOutput reads a rendered PNG, checks it decodes, and returns its
// SHA-256 and size. Used by the startup reconciler to detect frames whose
// database row says "succeeded" but whose file is missing, empty, or corrupt.
func VerifyOutput(path string) (string, int64, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", 0, err
	}
	sum, err := VerifyOutputBytes(data)
	return sum, int64(len(data)), err
}

// VerifyOutputBytes validates in-memory PNG bytes and returns their SHA-256.
func VerifyOutputBytes(data []byte) (string, error) {
	if len(data) == 0 {
		return "", fmt.Errorf("output is empty")
	}
	cfg, err := png.DecodeConfig(bytes.NewReader(data))
	if err != nil {
		return "", fmt.Errorf("not a valid png: %w", err)
	}
	if cfg.Width <= 0 || cfg.Height <= 0 {
		return "", fmt.Errorf("png has invalid dimensions")
	}
	if _, err := png.Decode(bytes.NewReader(data)); err != nil {
		return "", fmt.Errorf("png not fully decodable: %w", err)
	}
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:]), nil
}
