// Package compose performs real local PNG layer compositing: layers are
// decoded from PNG, alpha-blended ("over" operator, straight alpha) onto a
// transparent RGBA canvas in the manifest's explicit layer order, and
// encoded back to PNG. No placeholder or fixed images are ever emitted.
package compose

import (
	"bytes"
	"fmt"
	"image"
	"image/color"
	"image/png"

	"github.com/vfxqueue/renderq/internal/domain"
)

// LayerInput is one resolved layer: its PNG bytes plus placement.
type LayerInput struct {
	Spec    domain.Layer
	PNG     []byte
	Width   int
	Height  int
	X, Y    int
	Opacity float64
}

// RenderFrame composites the supplied layers onto a width x height canvas
// and returns the encoded PNG plus its dimensions. Layers are painted in
// slice order (the manifest's Order); every pixel is computed by the
// Porter-Duff "source over destination" formula.
//
// outA = srcA + dstA*(1-srcA)
// outC = (srcC*srcA + dstC*dstA*(1-srcA)) / outA
//
// where srcA additionally carries the layer's 0..1 Opacity.
func RenderFrame(canvasW, canvasH int, layers []LayerInput) ([]byte, error) {
	if canvasW <= 0 || canvasH <= 0 {
		return nil, fmt.Errorf("canvas size must be positive")
	}
	dst := image.NewNRGBA(image.Rect(0, 0, canvasW, canvasH)) // starts fully transparent (straight alpha)

	for li := range layers {
		in := layers[li]
		if in.Opacity == 0 {
			continue
		}
		img, err := decodePNG(in.PNG)
		if err != nil {
			return nil, fmt.Errorf("layer %q: decode png: %w", in.Spec.ID, err)
		}
		b := img.Bounds()
		if b.Dx() != in.Width || b.Dy() != in.Height {
			return nil, fmt.Errorf("layer %q: image is %dx%d but version snapshot recorded %dx%d",
				in.Spec.ID, b.Dx(), b.Dy(), in.Width, in.Height)
		}
		if err := blitOver(dst, img, in.X, in.Y, in.Opacity); err != nil {
			return nil, fmt.Errorf("layer %q: %w", in.Spec.ID, err)
		}
	}

	var buf bytes.Buffer
	if err := png.Encode(&buf, dst); err != nil {
		return nil, fmt.Errorf("encode png: %w", err)
	}
	return buf.Bytes(), nil
}

// FrameLayers projects the manifest's ordered layers onto a specific frame,
// applying per-frame x/y offsets. Missing frames offsets fall back to base.
func FrameLayers(m *domain.Manifest, frameNo int, blob map[string][]byte, resolved map[string]domain.ResolvedAsset) []LayerInput {
	out := make([]LayerInput, 0, len(m.Layers))
	for _, l := range m.Layers {
		r, ok := resolved[l.ID]
		if !ok {
			continue
		}
		data, ok := blob[r.SHA256]
		if !ok {
			continue
		}
		x, y := l.X, l.Y
		if dx, ok := l.FrameOffsetX[frameNo]; ok {
			x += dx
		}
		if dy, ok := l.FrameOffsetY[frameNo]; ok {
			y += dy
		}
		op := l.Opacity
		if op == 0 {
			op = 1
		}
		out = append(out, LayerInput{
			Spec: l, PNG: data, Width: r.Width, Height: r.Height,
			X: x, Y: y, Opacity: op,
		})
	}
	return out
}

func decodePNG(data []byte) (image.Image, error) {
	if len(data) == 0 {
		return nil, fmt.Errorf("empty png data")
	}
	img, err := png.Decode(bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	return img, nil
}

// blitOver composites src onto dst at (ox, oy) with an extra coverage
// multiplier opacity in [0,1]. Pixels outside the canvas are skipped
// (validation already guarantees full containment).
func blitOver(dst *image.NRGBA, src image.Image, ox, oy int, opacity float64) error {
	if opacity < 0 || opacity > 1 {
		return fmt.Errorf("opacity %g out of range", opacity)
	}
	sb := src.Bounds()
	db := dst.Bounds()
	for sy := sb.Min.Y; sy < sb.Max.Y; sy++ {
		for sx := sb.Min.X; sx < sb.Max.X; sx++ {
			px := ox + (sx - sb.Min.X)
			py := oy + (sy - sb.Min.Y)
			if !image.Pt(px, py).In(db) {
				continue
			}
			// straight-alpha components in 0..1
			sr, sg, sb2, sa := rgbaAt(src, sx, sy)
			srcA := sa * opacity
			if srcA == 0 {
				continue
			}

			i := dst.PixOffset(px, py)
			dstA := float64(dst.Pix[i+3]) / 255.0
			outA := srcA + dstA*(1-srcA)
			if outA == 0 {
				continue
			}
			blend := func(sc float64, dp uint8) uint8 {
				dc := float64(dp) / 255.0
				outC := (sc*srcA + dc*dstA*(1-srcA)) / outA
				v := int(outC*255 + 0.5)
				if v > 255 {
					v = 255
				}
				return uint8(v)
			}
			dst.Pix[i+0] = blend(sr, dst.Pix[i+0])
			dst.Pix[i+1] = blend(sg, dst.Pix[i+1])
			dst.Pix[i+2] = blend(sb2, dst.Pix[i+2])
			dst.Pix[i+3] = uint8(outA*255 + 0.5)
		}
	}
	return nil
}

// rgbaAt returns straight-alpha, non-premultiplied RGBA components in 0..1.
//
// color.Color.RGBA() always reports ALPHA-PREMULTIPLIED components, even for
// image.NRGBA, so it cannot be used directly. We convert through the NRGBA
// model and read its fields, which are straight alpha.
func rgbaAt(img image.Image, x, y int) (r, g, b, a float64) {
	nc := color.NRGBAModel.Convert(img.At(x, y)).(color.NRGBA)
	return float64(nc.R) / 255.0,
		float64(nc.G) / 255.0,
		float64(nc.B) / 255.0,
		float64(nc.A) / 255.0
}
