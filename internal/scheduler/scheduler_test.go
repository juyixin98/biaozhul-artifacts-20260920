package scheduler

import (
	"testing"

	"github.com/example/gpu-placement/internal/topology"
)

func twoNodeCluster() topology.Spec {
	return topology.Spec{
		Devices: []topology.Device{
			{ID: "gpu0", MemoryMB: 24576, NUMANode: 0},
			{ID: "gpu1", MemoryMB: 24576, NUMANode: 0},
			{ID: "gpu2", MemoryMB: 24576, NUMANode: 1},
			{ID: "gpu3", MemoryMB: 24576, NUMANode: 1},
		},
		DefaultSameNUMACost:  1,
		DefaultCrossNUMACost: 10,
	}
}

func TestAllocateBeforeConfigure(t *testing.T) {
	s := New()
	d := s.Allocate(TaskRequest{TaskID: "t", Replicas: 1, MemoryPerReplicaMB: 1024})
	if d.Reject == nil || d.Reject.Reason != ReasonNoCluster {
		t.Fatalf("未配置集群应拒绝 NO_CLUSTER，实际 %+v", d)
	}
	if s.HasCluster() {
		t.Fatal("未配置时 HasCluster 应为 false")
	}
}

func TestSharedDeviceMemoryAccounting(t *testing.T) {
	s := New()
	if err := s.Configure(twoNodeCluster()); err != nil {
		t.Fatal(err)
	}

	// 两个小任务可以共享同一张卡（显存累加）。
	d1 := s.Allocate(TaskRequest{TaskID: "a", Replicas: 1, MemoryPerReplicaMB: 8192})
	d2 := s.Allocate(TaskRequest{TaskID: "b", Replicas: 1, MemoryPerReplicaMB: 8192})
	if d1.Place == nil || d2.Place == nil {
		t.Fatalf("两个任务都应成功: %+v / %+v", d1.Reject, d2.Reject)
	}
	if d1.Place.Assignments[0].DeviceID != d2.Place.Assignments[0].DeviceID {
		t.Fatalf("单卡任务应共享字典序最小的 gpu0: %s vs %s",
			d1.Place.Assignments[0].DeviceID, d2.Place.Assignments[0].DeviceID)
	}

	view := s.View()
	var gpu0 *DeviceView
	for i := range view.Devices {
		if view.Devices[i].DeviceID == "gpu0" {
			gpu0 = &view.Devices[i]
		}
	}
	if gpu0 == nil || gpu0.UsedMemoryMB != 16384 || gpu0.FreeMemoryMB != 8192 {
		t.Fatalf("gpu0 显存账目错误: %+v", gpu0)
	}
	if len(gpu0.TaskIDs) != 2 {
		t.Fatalf("gpu0 应被两个任务共享: %+v", gpu0.TaskIDs)
	}

	// 释放一个任务后账目回滚。
	if !s.Release("a") {
		t.Fatal("释放 a 失败")
	}
	view = s.View()
	for _, d := range view.Devices {
		if d.DeviceID == "gpu0" && d.UsedMemoryMB != 8192 {
			t.Fatalf("释放后 gpu0 已用应为 8192，实际 %d", d.UsedMemoryMB)
		}
	}
	if s.Release("a") {
		t.Fatal("重复释放应返回 false")
	}
}

func TestReconfigureBlockedWhileTasksRunning(t *testing.T) {
	s := New()
	if err := s.Configure(twoNodeCluster()); err != nil {
		t.Fatal(err)
	}
	if d := s.Allocate(TaskRequest{TaskID: "x", Replicas: 1, MemoryPerReplicaMB: 1024}); d.Place == nil {
		t.Fatalf("分配失败: %+v", d.Reject)
	}
	if err := s.Configure(twoNodeCluster()); err == nil {
		t.Fatal("有在运行任务时重配应报错")
	}
	if !s.Release("x") {
		t.Fatal("释放失败")
	}
	if err := s.Configure(twoNodeCluster()); err != nil {
		t.Fatalf("释放后重配应成功: %v", err)
	}
	if len(s.Tasks()) != 0 {
		t.Fatal("重配后任务表应清空")
	}
}

func TestDeterministicAcrossRuns(t *testing.T) {
	// 同样的输入序列，两个独立调度器必须给出完全一致的结果。
	run := func() []Placement {
		s := New()
		if err := s.Configure(twoNodeCluster()); err != nil {
			t.Fatal(err)
		}
		for _, req := range []TaskRequest{
			{TaskID: "t1", Replicas: 2, MemoryPerReplicaMB: 4096},
			{TaskID: "t2", Replicas: 2, MemoryPerReplicaMB: 4096},
			{TaskID: "t3", Replicas: 1, MemoryPerReplicaMB: 4096},
		} {
			if d := s.Allocate(req); d.Place == nil {
				t.Fatalf("分配失败: %+v", d.Reject)
			}
		}
		return s.Tasks()
	}
	a, b := run(), run()
	if len(a) != len(b) {
		t.Fatal("任务数不一致")
	}
	for i := range a {
		if a[i].TaskID != b[i].TaskID || a[i].Cost != b[i].Cost ||
			len(a[i].Assignments) != len(b[i].Assignments) {
			t.Fatalf("第 %d 个任务结果不一致", i)
		}
		for j := range a[i].Assignments {
			if a[i].Assignments[j] != b[i].Assignments[j] {
				t.Fatalf("任务 %s 第 %d 个分配不一致: %+v vs %+v",
					a[i].TaskID, j, a[i].Assignments[j], b[i].Assignments[j])
			}
		}
	}
}
