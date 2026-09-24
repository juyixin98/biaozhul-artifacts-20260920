// Command genfixtures populates a registry directory with deterministic OCI
// repositories used by the README walkthrough and as example inputs. Every
// blob is real: digests are computed over the actual bytes with SHA-256 and
// declared sizes are the actual byte counts.
//
//	go run ./cmd/genfixtures -registry ./registry
package main

import (
	"flag"
	"fmt"
	"log"
	"os"

	"github.example.com/ocimultipick/internal/blobstore"
	"github.example.com/ocimultipick/internal/fixture"
	"github.example.com/ocimultipick/internal/oci"
)

func main() {
	registry := flag.String("registry", "./registry", "registry directory to populate")
	flag.Parse()

	if err := os.RemoveAll(*registry); err != nil {
		log.Fatal(err)
	}
	st := blobstore.New(*registry)

	if err := buildTwoArch(st); err != nil {
		log.Fatal(err)
	}
	if err := buildBadConfig(st); err != nil {
		log.Fatal(err)
	}
	if err := buildMissingLayer(st); err != nil {
		log.Fatal(err)
	}
	if err := buildAmbiguous(st); err != nil {
		log.Fatal(err)
	}
	if err := buildArmVariants(st); err != nil {
		log.Fatal(err)
	}
	if err := buildTagMove(st); err != nil {
		log.Fatal(err)
	}

	log.Printf("fixtures written to %s", *registry)
}

func image(b *fixture.Builder, name, goos, arch, variant string, layerPayload []byte) (oci.Descriptor, error) {
	layerContent := fixture.LayerContent(name, layerPayload)
	layer, err := b.AddLayer(name, layerContent)
	if err != nil {
		return oci.Descriptor{}, err
	}
	cfg, err := b.AddConfig(goos, arch, variant, []string{fixture.DiffIDUncompressed(layerContent)})
	if err != nil {
		return oci.Descriptor{}, err
	}
	return b.AddManifest(cfg, []oci.Descriptor{layer})
}

func buildTwoArch(st *blobstore.Store) error {
	const repo = "demo/two-arch"
	b := fixture.NewBuilder(st, repo)

	amd, err := image(b, "rootfs-amd64", "linux", "amd64", "", []byte("binaries for x86_64"))
	if err != nil {
		return err
	}
	amd.Platform = &oci.Platform{OS: "linux", Architecture: "amd64"}

	arm, err := image(b, "rootfs-arm64", "linux", "arm64", "", []byte("binaries for aarch64"))
	if err != nil {
		return err
	}
	arm.Platform = &oci.Platform{OS: "linux", Architecture: "arm64"}

	root, _, err := b.AddIndex([]oci.Descriptor{amd, arm})
	if err != nil {
		return err
	}
	if err := b.Tag("v1", root); err != nil {
		return err
	}
	if err := b.Tag("latest", root); err != nil {
		return err
	}
	fmt.Printf("repo %-22s tag v1/latest -> %s (linux/amd64 + linux/arm64)\n", repo, root.Digest)
	return nil
}

func buildBadConfig(st *blobstore.Store) error {
	const repo = "demo/bad-config"
	b := fixture.NewBuilder(st, repo)

	// The index advertises linux/arm64 but the config blob inside the image
	// declares linux/386: verification must reject the mismatch.
	arm, err := image(b, "rootfs-wrongcfg", "linux", "386", "", []byte("i386 bytes"))
	if err != nil {
		return err
	}
	arm.Platform = &oci.Platform{OS: "linux", Architecture: "arm64"}

	amd, err := image(b, "rootfs-ok", "linux", "amd64", "", []byte("x86_64 bytes"))
	if err != nil {
		return err
	}
	amd.Platform = &oci.Platform{OS: "linux", Architecture: "amd64"}

	root, _, err := b.AddIndex([]oci.Descriptor{arm, amd})
	if err != nil {
		return err
	}
	if err := b.Tag("latest", root); err != nil {
		return err
	}
	fmt.Printf("repo %-22s tag latest -> %s (arm64 entry's config lies: linux/386)\n", repo, root.Digest)
	return nil
}

func buildMissingLayer(st *blobstore.Store) error {
	const repo = "demo/missing-layer"
	b := fixture.NewBuilder(st, repo)

	layer, err := b.AddLayer("ghost", fixture.LayerContent("ghost", []byte("this blob will be deleted")))
	if err != nil {
		return err
	}
	cfg, err := b.AddConfig("linux", "amd64", "", []string{fixture.DiffIDUncompressed(fixture.LayerContent("ghost", []byte("this blob will be deleted")))})
	if err != nil {
		return err
	}
	manifest, err := b.AddManifest(cfg, []oci.Descriptor{layer})
	if err != nil {
		return err
	}
	// Delete the layer bytes but leave the manifest referencing them.
	if err := b.DeleteBlob(layer); err != nil {
		return err
	}
	manifest.Platform = &oci.Platform{OS: "linux", Architecture: "amd64"}

	root, _, err := b.AddIndex([]oci.Descriptor{manifest})
	if err != nil {
		return err
	}
	if err := b.Tag("latest", root); err != nil {
		return err
	}
	fmt.Printf("repo %-22s tag latest -> %s (manifest %s references a missing layer)\n",
		repo, root.Digest, manifest.Digest)
	return nil
}

func buildAmbiguous(st *blobstore.Store) error {
	const repo = "demo/ambiguous"
	b := fixture.NewBuilder(st, repo)

	first, err := image(b, "candidate-a", "linux", "amd64", "", []byte("candidate A"))
	if err != nil {
		return err
	}
	first.Platform = &oci.Platform{OS: "linux", Architecture: "amd64"}

	second, err := image(b, "candidate-b", "linux", "amd64", "", []byte("candidate B (different bytes)"))
	if err != nil {
		return err
	}
	second.Platform = &oci.Platform{OS: "linux", Architecture: "amd64"}

	root, _, err := b.AddIndex([]oci.Descriptor{first, second})
	if err != nil {
		return err
	}
	if err := b.Tag("latest", root); err != nil {
		return err
	}
	fmt.Printf("repo %-22s tag latest -> %s (two distinct linux/amd64 manifests)\n", repo, root.Digest)
	return nil
}

func buildArmVariants(st *blobstore.Store) error {
	const repo = "demo/arm-variants"
	b := fixture.NewBuilder(st, repo)

	v6, err := image(b, "armv6", "linux", "arm", "v6", []byte("armv6 rootfs"))
	if err != nil {
		return err
	}
	v6.Platform = &oci.Platform{OS: "linux", Architecture: "arm", Variant: "v6"}

	v7, err := image(b, "rootfs-v7", "linux", "arm", "v7", []byte("armv7 rootfs"))
	if err != nil {
		return err
	}
	v7.Platform = &oci.Platform{OS: "linux", Architecture: "arm", Variant: "v7"}

	v8, err := image(b, "rootfs-v8", "linux", "arm64", "v8", []byte("arm64v8 rootfs"))
	if err != nil {
		return err
	}
	v8.Platform = &oci.Platform{OS: "linux", Architecture: "arm64", Variant: "v8"}

	root, _, err := b.AddIndex([]oci.Descriptor{v6, v7, v8})
	if err != nil {
		return err
	}
	if err := b.Tag("latest", root); err != nil {
		return err
	}
	fmt.Printf("repo %-22s tag latest -> %s (arm/v6, arm/v7, arm64/v8)\n", repo, root.Digest)
	return nil
}

func buildTagMove(st *blobstore.Store) error {
	const repo = "demo/tag-move"
	b := fixture.NewBuilder(st, repo)

	amdV1, err := image(b, "v1-amd64", "linux", "amd64", "", []byte("release v1"))
	if err != nil {
		return err
	}
	amdV1.Platform = &oci.Platform{OS: "linux", Architecture: "amd64"}
	rootV1, _, err := b.AddIndex([]oci.Descriptor{amdV1})
	if err != nil {
		return err
	}

	amdV2, err := image(b, "v2-amd64", "linux", "amd64", "", []byte("release v2 - rebuilt"))
	if err != nil {
		return err
	}
	amdV2.Platform = &oci.Platform{OS: "linux", Architecture: "amd64"}
	rootV2, _, err := b.AddIndex([]oci.Descriptor{amdV2})
	if err != nil {
		return err
	}

	if err := b.Tag("v1", rootV1); err != nil {
		return err
	}
	if err := b.Tag("v2", rootV2); err != nil {
		return err
	}
	if err := b.Tag("latest", rootV1); err != nil {
		return err
	}
	fmt.Printf("repo %-22s tags v1->%s v2->%s latest->%s (move latest with PUT)\n",
		repo, rootV1.Digest, rootV2.Digest, rootV1.Digest)
	return nil
}
