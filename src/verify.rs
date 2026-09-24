//! 树结构全量校验：遍历整棵树检查
//! 1. 节点占用率（非根节点不得低于半满、不得超过容量）
//! 2. 内部节点：孩子数 = 分隔键数 + 1，分隔键严格递增
//! 3. 分隔键路由不变量：`max(左子树) < sep ≤ min(右子树)`
//! 4. 叶链：从最左叶沿 next 可串起全部叶，prev 反向一致，首尾为 NULL_PAGE
//! 5. 叶内键严格递增、所有叶子同深度
//!
//! 校验同时产出统计信息（高度、各层节点数与占用率），供 `/stats` 使用。

use std::io;

use serde::Serialize;

use crate::node::{Key, Node, PageId, NULL_PAGE};
use crate::tree::BpTree;

/// 单个节点的占用情况。
#[derive(Debug, Clone, Serialize)]
pub struct NodeStat {
    pub id: PageId,
    pub kind: &'static str,
    pub entries: usize,
    pub capacity: usize,
    /// 占用率（0.0~1.0）。
    pub fill_ratio: f64,
}

/// 校验报告；`ok = false` 时 `errors` 非空。
#[derive(Debug, Clone, Serialize)]
pub struct VerifyReport {
    pub ok: bool,
    pub errors: Vec<String>,
    pub height: usize,
    pub key_count: u64,
    pub leaf_count: usize,
    pub internal_count: usize,
    /// 每层节点数（0 = 根层）。
    pub levels: Vec<usize>,
    /// 叶节点占用率（entries/capacity）的最小/平均/最大值。
    pub leaf_fill_min: f64,
    pub leaf_fill_avg: f64,
    pub internal_fill_avg: f64,
    pub nodes: Vec<NodeStat>,
}

impl BpTree {
    /// 全量校验并返回报告（不返回 Result：结构问题体现在 `ok/errors`，
    /// 仅磁盘 IO 错误才返回 Err）。
    pub fn verify(&mut self) -> io::Result<VerifyReport> {
        let limits = self.limits();
        let mut rep = VerifyReport {
            ok: true,
            errors: Vec::new(),
            height: 0,
            key_count: 0,
            leaf_count: 0,
            internal_count: 0,
            levels: Vec::new(),
            leaf_fill_min: 1.0,
            leaf_fill_avg: 0.0,
            internal_fill_avg: 0.0,
            nodes: Vec::new(),
        };

        if self.root_id() == NULL_PAGE {
            if self.leftmost_id() != NULL_PAGE {
                rep.ok = false;
                rep.errors.push("空树但 leftmost 非空".to_string());
            }
            if self.key_count() != 0 {
                rep.ok = false;
                rep.errors.push("空树但 key_count 非零".to_string());
            }
            return Ok(rep);
        }

        // 迭代 DFS：(页号, 层数, 允许键下界（含）, 允许键上界（不含）)。
        // 分隔键允许等于右子树最小键（叶键副本），故上界为开区间。
        struct Frame {
            id: PageId,
            depth: usize,
            low: Option<Key>,
            high_exclusive: Option<Key>,
        }
        let mut stack = vec![Frame {
            id: self.root_id(),
            depth: 0,
            low: None,
            high_exclusive: None,
        }];
        let mut leaf_depths: Vec<usize> = Vec::new();
        let mut leaf_fill_sum: f64 = 0.0;
        let mut internal_fill_sum: f64 = 0.0;

        while let Some(fr) = stack.pop() {
            if fr.depth >= rep.levels.len() {
                rep.levels.resize(fr.depth + 1, 0);
            }
            rep.levels[fr.depth] += 1;

            let node = self.load_node(fr.id)?;
            let is_root = fr.id == self.root_id();
            match node {
                Node::Leaf(leaf) => {
                    rep.leaf_count += 1;
                    rep.key_count += leaf.keys.len() as u64;
                    leaf_depths.push(fr.depth);

                    if leaf.keys.len() > limits.leaf_max {
                        rep.ok = false;
                        rep.errors.push(format!(
                            "叶页 {} 溢出：{} 项 > 容量 {}",
                            fr.id,
                            leaf.keys.len(),
                            limits.leaf_max
                        ));
                    }
                    if !is_root && leaf.keys.len() < limits.leaf_min() {
                        rep.ok = false;
                        rep.errors.push(format!(
                            "非根叶页 {} 下溢：{} 项 < 半满下限 {}",
                            fr.id,
                            leaf.keys.len(),
                            limits.leaf_min()
                        ));
                    }
                    // 键严格递增 + 落在子树允许区间内。
                    for w in leaf.keys.windows(2) {
                        if w[0] >= w[1] {
                            rep.ok = false;
                            rep.errors.push(format!(
                                "叶页 {} 键未严格递增：{} >= {}",
                                fr.id, w[0], w[1]
                            ));
                        }
                    }
                    if let Some(k) = leaf.keys.first() {
                        if fr.low.is_some_and(|lo| *k < lo) {
                            rep.ok = false;
                            rep.errors.push(format!(
                                "叶页 {} 最小键 {} 越出下界 {:?}",
                                fr.id, k, fr.low
                            ));
                        }
                    }
                    if let Some(k) = leaf.keys.last() {
                        if fr.high_exclusive.is_some_and(|hi| *k >= hi) {
                            rep.ok = false;
                            rep.errors.push(format!(
                                "叶页 {} 最大键 {} 越出上界（应 < {:?}）",
                                fr.id, k, fr.high_exclusive
                            ));
                        }
                    }
                    let fill = leaf.keys.len() as f64 / limits.leaf_max as f64;
                    rep.leaf_fill_min = rep.leaf_fill_min.min(fill);
                    leaf_fill_sum += fill;
                    rep.nodes.push(NodeStat {
                        id: fr.id,
                        kind: "leaf",
                        entries: leaf.keys.len(),
                        capacity: limits.leaf_max,
                        fill_ratio: fill,
                    });
                }
                Node::Internal(internal) => {
                    rep.internal_count += 1;
                    let n = internal.separators.len();
                    if internal.children.len() != n + 1 {
                        rep.ok = false;
                        rep.errors.push(format!(
                            "内部页 {} 孩子数 {} != 分隔键数 {} + 1",
                            fr.id,
                            internal.children.len(),
                            n
                        ));
                    }
                    if n > limits.internal_max {
                        rep.ok = false;
                        rep.errors.push(format!(
                            "内部页 {} 溢出：{} 个分隔键 > 容量 {}",
                            fr.id, n, limits.internal_max
                        ));
                    }
                    if !is_root && n < limits.internal_min_seps() {
                        rep.ok = false;
                        rep.errors.push(format!(
                            "非根内部页 {} 下溢：{} 个分隔键 < 半满下限 {}",
                            fr.id,
                            n,
                            limits.internal_min_seps()
                        ));
                    }
                    for w in internal.separators.windows(2) {
                        if w[0] >= w[1] {
                            rep.ok = false;
                            rep.errors.push(format!(
                                "内部页 {} 分隔键未严格递增：{} >= {}",
                                fr.id, w[0], w[1]
                            ));
                        }
                    }
                    // 分隔键必须落在本节点代表区间内（它是右子树最小键的副本，
                    // 故满足 low <= sep < high_exclusive）。
                    for s in &internal.separators {
                        if fr.low.is_some_and(|lo| *s < lo) {
                            rep.ok = false;
                            rep.errors.push(format!(
                                "内部页 {} 分隔键 {} 越出下界 {:?}",
                                fr.id, s, fr.low
                            ));
                        }
                        if fr.high_exclusive.is_some_and(|hi| *s >= hi) {
                            rep.ok = false;
                            rep.errors.push(format!(
                                "内部页 {} 分隔键 {} 越出上界（应 < {:?}）",
                                fr.id, s, fr.high_exclusive
                            ));
                        }
                    }
                    let fill = n as f64 / limits.internal_max as f64;
                    internal_fill_sum += fill;
                    rep.nodes.push(NodeStat {
                        id: fr.id,
                        kind: "internal",
                        entries: n,
                        capacity: limits.internal_max,
                        fill_ratio: fill,
                    });

                    // 孩子区间（下界闭、上界开）：
                    // children[0]      键 < separators[0]
                    // children[i]      separators[i-1] <= 键 < separators[i]
                    // children[n]      键 >= separators[n-1]
                    for (i, &child) in internal.children.iter().enumerate() {
                        let low = if i == 0 {
                            fr.low
                        } else {
                            Some(internal.separators[i - 1])
                        };
                        let high_exclusive =
                            if i == n { fr.high_exclusive } else { Some(internal.separators[i]) };
                        stack.push(Frame {
                            id: child,
                            depth: fr.depth + 1,
                            low,
                            high_exclusive,
                        });
                    }
                }
            }
        }

        // 所有叶子同深度。
        if let Some(d) = leaf_depths.first().copied() {
            if leaf_depths.iter().any(|x| *x != d) {
                rep.ok = false;
                rep.errors.push(format!("叶子深度不一致：{leaf_depths:?}"));
            }
            rep.height = d + 1;
        }

        // 用“实际子树极值”核对每个内部节点的分隔键：
        // 取每个孩子子树的最小/最大键，检查 max(左) < sep <= min(右)。
        self.check_separators(self.root_id(), &mut rep)?;

        // 叶链检查：从 leftmost 沿 next 走，与 DFS 收集到的叶集合对照；
        // 同时检查 prev 反向一致、键全局有序。
        self.check_leaf_chain(&mut rep)?;

        if rep.key_count != self.key_count() {
            rep.ok = false;
            rep.errors.push(format!(
                "实际键数 {} 与元数据 key_count {} 不一致",
                rep.key_count,
                self.key_count()
            ));
        }

        if rep.leaf_count > 0 {
            rep.leaf_fill_avg = leaf_fill_sum / rep.leaf_count as f64;
        }
        if rep.internal_count > 0 {
            rep.internal_fill_avg = internal_fill_sum / rep.internal_count as f64;
        }
        // 空树以外，根为叶时 leaf_count=1、min 保持 1.0 初值即可。
        if rep.leaf_count == 0 {
            rep.leaf_fill_min = 0.0;
        }
        Ok(rep)
    }

    /// 递归核对分隔键与相邻子树实际极值：max(left) < sep <= min(right)。
    /// 返回该子树 (最小键, 最大键)；空孩子不会出现。
    fn check_separators(
        &mut self,
        id: PageId,
        rep: &mut VerifyReport,
    ) -> io::Result<(Key, Key)> {
        match self.load_node(id)? {
            Node::Leaf(leaf) => Ok((*leaf.keys.first().unwrap(), *leaf.keys.last().unwrap())),
            Node::Internal(internal) => {
                let mut bounds = Vec::with_capacity(internal.children.len());
                for &child in &internal.children {
                    bounds.push(self.check_separators(child, rep)?);
                }
                for (i, sep) in internal.separators.iter().enumerate() {
                    let (lmin, lmax) = bounds[i];
                    let (rmin, rmax) = bounds[i + 1];
                    if !(lmax < *sep) {
                        rep.ok = false;
                        rep.errors.push(format!(
                            "内部页 {} 分隔键 {} 不满足 左子树最大键 {} < sep",
                            id, sep, lmax
                        ));
                    }
                    if !(*sep <= rmin) {
                        rep.ok = false;
                        rep.errors.push(format!(
                            "内部页 {} 分隔键 {} 不满足 sep <= 右子树最小键 {}",
                            id, sep, rmin
                        ));
                    }
                    if !(lmax < rmax) || !(lmin <= rmin) {
                        rep.ok = false;
                        rep.errors.push(format!(
                            "内部页 {} 相邻子树区间倒置 [{lmin},{lmax}] / [{rmin},{rmax}]",
                            id
                        ));
                    }
                }
                Ok((bounds.first().unwrap().0, bounds.last().unwrap().1))
            }
        }
    }

    /// 沿叶链检查：起点必须是 leftmost；next/prev 双向一致；首尾为 NULL；
    /// 串联叶数等于 DFS 叶数；叶间键全局有序（前叶最大键 < 后叶最小键）。
    fn check_leaf_chain(&mut self, rep: &mut VerifyReport) -> io::Result<()> {
        if self.leftmost_id() == NULL_PAGE {
            return Ok(());
        }
        let mut id = self.leftmost_id();
        let mut prev_id = NULL_PAGE;
        let mut count = 0usize;
        let mut last_key: Option<Key> = None;
        loop {
            match self.load_node(id)? {
                Node::Internal(_) => {
                    rep.ok = false;
                    rep.errors.push(format!("叶链上的页 {id} 是内部节点"));
                    return Ok(());
                }
                Node::Leaf(leaf) => {
                    if leaf.keys.is_empty() {
                        rep.ok = false;
                        rep.errors.push(format!("叶链上的非根叶页 {id} 为空"));
                        return Ok(());
                    }
                    if leaf.prev != prev_id {
                        rep.ok = false;
                        rep.errors.push(format!(
                            "叶页 {id} 的 prev={} 与实际前驱 {prev_id} 不一致",
                            leaf.prev
                        ));
                    }
                    if count == 0 && leaf.prev != NULL_PAGE {
                        rep.ok = false;
                        rep.errors.push(format!("最左叶 {id} 的 prev 非空"));
                    }
                    if let Some(last) = last_key {
                        if last >= leaf.keys[0] {
                            rep.ok = false;
                            rep.errors.push(format!(
                                "叶链在页 {id} 处失序：前叶最大键 {last} >= 本叶最小键 {}",
                                leaf.keys[0]
                            ));
                        }
                    }
                    last_key = Some(*leaf.keys.last().unwrap());
                    count += 1;
                    match leaf.next {
                        NULL_PAGE => {
                            if count != rep.leaf_count {
                                rep.ok = false;
                                rep.errors.push(format!(
                                    "叶链串联 {count} 个叶，但树上共有 {} 个叶",
                                    rep.leaf_count
                                ));
                            }
                            // 反向抽查：最左叶必须等于 leftmost 已由起点保证。
                            break;
                        }
                        next => {
                            prev_id = id;
                            id = next;
                        }
                    }
                }
            }
        }
        Ok(())
    }

    // verify 专用读节点（tree 模块的 load 为 crate 内可见）。
    fn load_node(&mut self, id: PageId) -> io::Result<Node> {
        self.load(id)
    }
}
