package syncer_test

import (
	"encoding/hex"
	"path/filepath"
	"testing"
	"time"

	"nodesync/internal/chain"
	"nodesync/internal/harness"
	"nodesync/internal/storage"
	"nodesync/internal/syncer"
)

const testTip = uint64(63) // 64 个区块，段大小 8 => 8 段

// newFixture 生成确定性测试链与检查点 0,16,32,48,63。
func newFixture(t *testing.T) *chain.Fixture {
	t.Helper()
	blocks := chain.GenerateChain(testTip)
	sample := chain.SampleForChain(blocks, []uint64{0, 16, 32, 48, 63})
	return &chain.Fixture{ChainTip: testTip, Blocks: blocks, TrustedSample: sample}
}

type cluster struct {
	fx    *chain.Fixture
	nodes []*harness.Node
	peers []*harness.Peer
}

func startCluster(t *testing.T, fx *chain.Fixture, faults map[string]harness.FaultSpec, order []string) *cluster {
	t.Helper()
	c := &cluster{fx: fx}
	created := map[string]*harness.Node{}
	for _, id := range order {
		n, err := harness.NewNode(id, fx, faults[id])
		if err != nil {
			t.Fatalf("创建节点 %s: %v", id, err)
		}
		go n.Serve()
		created[id] = n
		c.nodes = append(c.nodes, n)
	}
	for _, id := range order {
		p, err := harness.Dial(id, created[id].Addr())
		if err != nil {
			t.Fatalf("拨号节点 %s: %v", id, err)
		}
		c.peers = append(c.peers, p)
	}
	t.Cleanup(func() {
		for _, p := range c.peers {
			_ = p.Close()
		}
		for _, n := range c.nodes {
			n.Stop()
		}
	})
	return c
}

// 标准三节点故障配置（段大小 8，共 8 段；首轮源 = 段索引 % 3：
// 段 0,3,6 -> node-a；段 1,4,7 -> node-b；段 2,5 -> node-c）。
// 每个首轮节点都在其首轮段注入故障，保证故障必被触发、随后换源成功：
//
//	node-a: 段0 超时、段3 payload 损坏、段6 gRPC 错误，并虚高宣称 +10
//	node-b: 段1 错误父哈希、段4 短段、段7(尾段) 超时
//	node-c: 段2 诚实快速（中段缺口场景的诚实源）、段5 超时
func standardFaults() map[string]harness.FaultSpec {
	return map[string]harness.FaultSpec{
		"node-a": {
			TimeoutRanges:        []harness.Range{{Start: 0, End: 8}},
			TimeoutDelay:         2 * time.Second,
			CorruptPayloadRanges: []harness.Range{{Start: 24, End: 32}},
			ErrorRanges:          []harness.Range{{Start: 48, End: 56}},
			AdvertisedTipInflate: 10,
		},
		"node-b": {
			CorruptParentRanges: []harness.Range{{Start: 8, End: 16}},
			ShortRanges:         []harness.Range{{Start: 32, End: 40}},
			TimeoutRanges:       []harness.Range{{Start: 56, End: 64}},
			TimeoutDelay:        2 * time.Second,
		},
		"node-c": {
			TimeoutRanges: []harness.Range{{Start: 40, End: 48}},
			TimeoutDelay:  2 * time.Second,
		},
	}
}

func testStore(t *testing.T) *storage.Store {
	t.Helper()
	st, err := storage.Open(stFilePath(t))
	if err != nil {
		t.Fatalf("打开存储: %v", err)
	}
	t.Cleanup(func() { _ = st.Close() })
	return st
}

func stFilePath(t *testing.T) string {
	t.Helper()
	return filepath.Join(t.TempDir(), "sync.db")
}

func baseConfig(c *cluster, st *storage.Store) syncer.Config {
	return syncer.Config{
		Peers:         c.peers,
		Store:         st,
		Sample:        c.fx.TrustedSample,
		SegmentSize:   8,
		MaxParallel:   4,
		WindowSegs:    8,
		MaxSegRetries: 4,
		RPCTimeout:    300 * time.Millisecond,
	}
}

// assertFullChain 校验数据库中的链 0..tip 与夹具逐块一致。
func assertFullChain(t *testing.T, st *storage.Store, fx *chain.Fixture) {
	t.Helper()
	blocks, err := st.BlocksMap()
	if err != nil {
		t.Fatalf("读取区块: %v", err)
	}
	for h := uint64(0); h <= fx.ChainTip; h++ {
		b, ok := blocks[h]
		if !ok {
			t.Fatalf("高度 %d 缺失：链不完整", h)
		}
		want := fx.Blocks[h]
		if string(b.Payload) != string(want.Payload) {
			t.Fatalf("高度 %d payload 不一致", h)
		}
		if fmtHash(b) != fmtHash(want) {
			t.Fatalf("高度 %d 哈希与夹具不一致", h)
		}
		if err := b.VerifyCrypto(); err != nil {
			t.Fatalf("高度 %d 密码学校验失败: %v", h, err)
		}
		if h > 0 && fmtHash(blocks[h-1]) != toHex(b.ParentHash) {
			t.Fatalf("高度 %d 父链接断裂", h)
		}
	}
}

func fmtHash(b *chain.Block) string { return toHex(b.Hash()) }
func toHex(b []byte) string         { return hex.EncodeToString(b) }
