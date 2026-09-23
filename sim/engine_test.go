package sim

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestReliableScenario(t *testing.T) {
	sc := &Scenario{
		Name:     "reliable",
		Replicas: []string{"r1", "r2"},
		Network:  Network{MinDelay: 1, MaxDelay: 3},
		Events: []EventSpec{
			{At: 1, Kind: "add", Replica: "r1", Value: "x"},
			{At: 2, Kind: "sync", Replica: "r1", To: "r2"},
		},
		ExpectedValues: []string{"x"},
	}
	res := Run(sc)
	if !res.Converged || !res.MatchesExpected {
		t.Fatalf("可靠网络应收敛到 {x}: converged=%v values r2=%v", res.Converged, res.Nodes["r2"].Values)
	}
	if res.Stats.MessagesSent != 1 || res.Stats.MessagesDropped != 0 || res.Stats.MessagesDelivered != 1 {
		t.Fatalf("消息计数异常: %+v", res.Stats)
	}
}

func TestDropAndDeliver(t *testing.T) {
	// 50% 丢包、单条消息：穷举种子直到找到“丢”和“收”两类，验证两种语义都可达。
	sawDropped, sawDelivered := false, false
	for seed := uint64(1); seed <= 100 && !(sawDropped && sawDelivered); seed++ {
		sc := &Scenario{
			Name:     "drop",
			Seed:     seed,
			Replicas: []string{"r1", "r2"},
			Network:  Network{DropProb: 0.5, MinDelay: 1, MaxDelay: 1},
			Events: []EventSpec{
				{At: 1, Kind: "add", Replica: "r1", Value: "x"},
				{At: 2, Kind: "sync", Replica: "r1", To: "r2"},
			},
		}
		res := Run(sc)
		if res.Stats.MessagesDropped == 1 {
			sawDropped = true
			if res.Converged {
				t.Fatalf("seed=%d 唯一消息被丢却报告收敛", seed)
			}
		}
		if res.Stats.MessagesDelivered == 1 {
			sawDelivered = true
			if !res.Converged {
				t.Fatalf("seed=%d 消息送达却未收敛", seed)
			}
		}
	}
	if !sawDropped || !sawDelivered {
		t.Fatalf("未覆盖两种结果: dropped=%v delivered=%v", sawDropped, sawDelivered)
	}
}

func TestDuplicateDeliversTwice(t *testing.T) {
	found := false
	for seed := uint64(1); seed <= 50 && !found; seed++ {
		sc := &Scenario{
			Name:     "dup",
			Seed:     seed,
			Replicas: []string{"r1", "r2"},
			Network:  Network{DuplicateProb: 1.0, MinDelay: 1, MaxDelay: 4},
			Events: []EventSpec{
				{At: 1, Kind: "add", Replica: "r1", Value: "x"},
				{At: 2, Kind: "sync", Replica: "r1", To: "r2"},
			},
		}
		res := Run(sc)
		if res.Stats.DuplicatesMade == 1 && res.Stats.MessagesDelivered == 2 {
			found = true
			// 第一份合并带来 x，第二份必须是冗余幂等合并。
			var delivers []TraceEntry
			for _, e := range res.Trace {
				if e.Kind == "deliver" {
					delivers = append(delivers, e)
				}
			}
			nonRedundant, redundant := 0, 0
			for _, d := range delivers {
				if d.Redundant {
					redundant++
				} else {
					nonRedundant++
				}
			}
			if nonRedundant != 1 || redundant != 1 {
				t.Fatalf("seed=%d 两份投递应为 1 次有效 + 1 次冗余，得到 %d/%d", seed, nonRedundant, redundant)
			}
			if !res.Converged {
				t.Fatalf("seed=%d 重复投递应仍然收敛", seed)
			}
		}
	}
	if !found {
		t.Fatal("100% 复制概率下未产生重复投递")
	}
}

func TestReorderObserved(t *testing.T) {
	sc := &Scenario{
		Name:     "reorder",
		Seed:     1,
		Replicas: []string{"r1", "r2"},
		Network:  Network{ReorderProb: 1.0, MinDelay: 5, MaxDelay: 5},
		Events: []EventSpec{
			{At: 1, Kind: "add", Replica: "r1", Value: "x"},
			{At: 2, Kind: "sync", Replica: "r1", To: "r2"}, // 先入队，到达 7
			{At: 3, Kind: "sync", Replica: "r1", To: "r2"}, // 强制早于前者
		},
	}
	res := Run(sc)
	if res.Stats.ForcedReorders == 0 || res.Stats.Reorders == 0 {
		t.Fatalf("应观察到强制乱序: %+v", res.Stats)
	}
	if !res.Converged {
		t.Fatal("乱序不影响最终收敛")
	}
	// 第二条 send 必须早于第一条 deliver（后发先至）。
	send2At, firstDeliverAt := -1, -1
	for _, e := range res.Trace {
		if e.Kind == "send" && e.SendSeq == 2 {
			send2At = e.At
		}
		if e.Kind == "deliver" && firstDeliverAt == -1 {
			firstDeliverAt = e.At
		}
	}
	if send2At < 0 || firstDeliverAt < 0 || firstDeliverAt <= send2At {
		t.Fatalf("乱序时序错误 send2=%d firstDeliver=%d", send2At, firstDeliverAt)
	}
}

func TestDeterministicReplay(t *testing.T) {
	sc := &Scenario{
		Name:     "det",
		Seed:     7,
		Replicas: []string{"r1", "r2", "r3"},
		Network:  Network{DropProb: 0.2, DuplicateProb: 0.2, ReorderProb: 0.2, MinDelay: 1, MaxDelay: 6},
		Events: []EventSpec{
			{At: 1, Kind: "add", Replica: "r1", Value: "x"},
			{At: 2, Kind: "gossip", Replica: "r1"},
			{At: 3, Kind: "add", Replica: "r2", Value: "y"},
			{At: 10, Kind: "gossip", Replica: "r2"},
		},
	}
	b1, _ := json.Marshal(Run(sc))
	b2, _ := json.Marshal(Run(sc))
	if string(b1) != string(b2) {
		t.Fatal("相同输入两次运行结果不一致")
	}
}

func TestValidation(t *testing.T) {
	cases := []struct {
		name string
		sc   Scenario
		want string
	}{
		{"空副本", Scenario{}, "replicas"},
		{"重复副本", Scenario{Replicas: []string{"a", "a"}}, "重复"},
		{"坏丢包率", Scenario{Replicas: []string{"a"}, Network: Network{DropProb: 1}}, "drop_prob"},
		{"坏延迟", Scenario{Replicas: []string{"a"}, Network: Network{MinDelay: 5, MaxDelay: 1}}, "max_delay"},
		{"未知副本", Scenario{Replicas: []string{"a"}, Events: []EventSpec{{Kind: "add", Replica: "x"}}}, "未知副本"},
		{"缺 value", Scenario{Replicas: []string{"a"}, Events: []EventSpec{{Kind: "add", Replica: "a"}}}, "value"},
		{"坏 kind", Scenario{Replicas: []string{"a"}, Events: []EventSpec{{Kind: "bogus", Replica: "a"}}}, "kind"},
		{"sync 自己", Scenario{Replicas: []string{"a", "b"}, Events: []EventSpec{{Kind: "sync", Replica: "a", To: "a"}}}, "自己"},
	}
	for _, c := range cases {
		err := c.sc.Validate()
		if err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: 期望包含 %q 的错误，得到 %v", c.name, c.want, err)
		}
	}
}
