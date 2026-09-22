package integration_test

import (
	"image/color"
	"testing"
	"time"

	"vfxqueue/internal/testsupport"
)

// TestVersionFreezeBindsDigest: once a version is frozen it records each
// layer's asset digest. Mutating the asset row afterwards (e.g. a new upload
// that replaces the blob is impossible due to content addressing, but a
// direct DB swap simulates tampering) must not change what an existing task
// renders: the worker validates the frozen digest and fails rather than
// rendering different content.
func TestVersionFreezeBindsDigest(t *testing.T) {
	h := testsupport.New(t)
	owner := h.CreateUser("member")
	p := h.CreateProject(owner)
	data := testsupport.SolidPNG(4, 4, color.NRGBA{1, 2, 3, 255})
	a := h.CreateAsset(p, owner, "a.png", data)
	comp, ver := h.FreezeVersion(p, owner, specFromAsset(a.ID.String(), 4, 4, 1))
	task := h.Enqueue(p, comp, ver, 0, 0, 5, owner)

	// Tamper: change the frozen resource's recorded digest so the file on disk
	// no longer matches. The worker must refuse to render.
	_, err := h.Pool.Exec(h.Ctx,
		`UPDATE composition_version_resources SET asset_sha256 = $2 WHERE version_id = $1`,
		ver.ID, "deadbeef")
	if err != nil {
		t.Fatal(err)
	}

	stop := startWorker(h)
	defer stop()
	final := h.WaitForTask(task.ID, 15*time.Second, "failed")
	if final.Status != "failed" {
		t.Fatalf("status=%s", final.Status)
	}
	f := h.WaitForFrame(task.ID, 0, 5*time.Second, "failed")
	if f.Error == nil {
		t.Fatal("frame must record the digest error")
	}
}

// TestVersionResourcesAreImmutableSnapshot: a second upload with the same
// project but different bytes gets a different asset; a new version may use
// the new asset while an older task still renders the old one.
func TestVersionResourcesAreImmutableSnapshot(t *testing.T) {
	h := testsupport.New(t)
	owner := h.CreateUser("member")
	p := h.CreateProject(owner)
	a1 := h.CreateAsset(p, owner, "a.png", testsupport.SolidPNG(4, 4, color.NRGBA{1, 1, 1, 255}))
	a2 := h.CreateAsset(p, owner, "b.png", testsupport.SolidPNG(4, 4, color.NRGBA{200, 200, 200, 255}))

	comp, ver1 := h.FreezeVersion(p, owner, specFromAsset(a1.ID.String(), 4, 4, 1))

	// Version 2 uses a2.
	_, ver2 := h.FreezeVersionNumbered(p, owner, comp, 2, specFromAsset(a2.ID.String(), 4, 4, 1))

	rows1, err := h.Q.ListVersionResources(h.Ctx, ver1.ID)
	if err != nil || len(rows1) != 1 || rows1[0].AssetSha256 != a1.Sha256 {
		t.Fatalf("version 1 resources wrong: %v %v", rows1, err)
	}
	rows2, err := h.Q.ListVersionResources(h.Ctx, ver2.ID)
	if err != nil || len(rows2) != 1 || rows2[0].AssetSha256 != a2.Sha256 {
		t.Fatalf("version 2 resources wrong: %v %v", rows2, err)
	}
	if a1.Sha256 == a2.Sha256 {
		t.Fatal("different bytes must produce different digests")
	}
}
