//! 磁盘 B+ 树：整数键（i64）-> u64 值。
//!
//! 设计要点：
//! - 叶节点按键升序存键值对，叶子之间用 `next` 页号串成单向有序链表。
//! - 内部节点只存路由分隔键（[`InternalNode`]），分隔键是“右子树最小键”的副本，
//!   路由规则为 `child_i` 容纳 `seps[i-1] <= key < seps[i]`。
//! - 插入：自顶向下定位到叶，溢出时一分为二，分隔键沿父链上推；根分裂使树高 +1。
//! - 删除：自底向上收缩，先尝试向兄弟借位（旋转），不行则与兄弟合并；
//!   根内部节点只剩一个孩子时塌缩，树高 -1，可一直降回单层叶根。

use crate::node::{alloc_leaf, load_node, store_node, InternalNode, LeafNode, Node};
use crate::pager::{PageId, Pager, NIL};

/// 一次删除递归的结果。
#[derive(Debug, Clone, Copy)]
struct Del {
    found: bool,
    /// 本节点（非根）是否已低于最低占用，需要父节点修复。
    underflow: bool,
}

/// 树的统计/占用信息（由 [`BPTree::verify`] 产出）。
#[derive(Debug, Clone, PartialEq)]
pub struct TreeStats {
    pub height: usize,
    pub internal_pages: usize,
    pub leaf_pages: usize,
    pub item_count: usize,
    /// 每个非根节点的占用率（叶：项数/叶容量；内部：孩子数/内部容量）。
    pub occupancies: Vec<f64>,
}

/// 建立在任意 [`Pager`] 之上的 B+ 树句柄。
pub struct BPTree<P: Pager> {
    pager: P,
}

impl<P: Pager> BPTree<P> {
    pub fn new(pager: P) -> Self {
        BPTree { pager }
    }

    pub fn page_size(&self) -> usize {
        self.pager.page_size()
    }

    pub fn leaf_capacity(&self) -> usize {
        Node::leaf_capacity(self.pager.page_size())
    }

    pub fn into_pager(self) -> P {
        self.pager
    }

    pub fn pager(&self) -> &P {
        &self.pager
    }

    fn leaf_min(&self) -> usize {
        Node::leaf_min_fill(self.pager.page_size())
    }

    fn internal_min(&self) -> usize {
        Node::internal_min_children(self.pager.page_size())
    }

    // ---------------- 查找 ----------------

    /// 沿内部节点下降到 `key` 所属（或应插入）的叶页。
    fn descend_leaf(&self, key: i64) -> PageId {
        let mut id = self.pager.root_page();
        loop {
            match load_node(&self.pager, id) {
                Node::Leaf(_) => return id,
                Node::Internal(i) => {
                    let p = i.child_index(key);
                    id = i.children[p];
                }
            }
        }
    }

    pub fn get(&self, key: i64) -> Option<u64> {
        let id = self.descend_leaf(key);
        let leaf = load_node(&self.pager, id).as_leaf().clone();
        match leaf.locate(key) {
            Ok(_) => None,
            Err(p) => Some(leaf.values[p]),
        }
    }

    /// 闭区间范围读，按升序返回，沿叶链顺序扫描。
    pub fn range(&self, lo: i64, hi: i64) -> Vec<(i64, u64)> {
        if lo > hi {
            return Vec::new();
        }
        let mut out = Vec::new();
        let mut id = self.descend_leaf(lo);
        while id != NIL {
            let leaf = load_node(&self.pager, id).as_leaf().clone();
            let start = leaf.keys.partition_point(|k| *k < lo);
            let mut passed_hi = false;
            for j in start..leaf.keys.len() {
                if leaf.keys[j] > hi {
                    passed_hi = true;
                    break;
                }
                out.push((leaf.keys[j], leaf.values[j]));
            }
            if passed_hi || leaf.next == NIL {
                break;
            }
            // 本叶所有键都 <= hi，继续沿叶链向右。
            id = leaf.next;
        }
        out
    }

    // ---------------- 插入 ----------------

    /// 插入或覆盖。返回 true 表示新插入，false 表示键已存在并更新了值。
    pub fn insert(&mut self, key: i64, value: u64) -> bool {
        let root = self.pager.root_page();
        let (inserted, split) = self.insert_rec(root, key, value);
        if let Some(split) = split {
            // 根分裂：新建一个内部根 [old_root, sep, right]
            let new_root = self.pager.alloc_page();
            let node = Node::Internal(InternalNode {
                children: vec![root, split.right],
                seps: vec![split.sep],
            });
            store_node(&mut self.pager, new_root, &node);
            self.pager.set_root_page(new_root);
        }
        inserted
    }

    /// 在子树插入；返回 (是否新插入, 若节点分裂则上推的分隔键与新右兄弟)。
    fn insert_rec(&mut self, id: PageId, key: i64, value: u64) -> (bool, Option<SplitUp>) {
        let mut node = load_node(&self.pager, id);
        match &mut node {
            Node::Leaf(leaf) => self.insert_leaf(id, leaf, key, value),
            Node::Internal(internal) => {
                let p = internal.child_index(key);
                let child = internal.children[p];
                let (inserted, split) = self.insert_rec(child, key, value);
                if let Some(split) = split {
                    internal.seps.insert(p, split.sep);
                    internal.children.insert(p + 1, split.right);
                    if internal.children.len() <= Node::internal_capacity(self.pager.page_size()) {
                        store_node(&mut self.pager, id, &node);
                        return (inserted, None);
                    }
                    // 内部节点溢出：取中间分隔键上推，左右各保留一半孩子。
                    let mid = internal.children.len() / 2;
                    let promote = internal.seps.remove(mid - 1);
                    let right_children: Vec<PageId> = internal.children.drain(mid..).collect();
                    let right_seps: Vec<i64> = internal.seps.drain(mid - 1..).collect();
                    store_node(&mut self.pager, id, &node); // 左半仍用原页
                    let right_id = self.pager.alloc_page();
                    store_node(
                        &mut self.pager,
                        right_id,
                        &Node::Internal(InternalNode {
                            children: right_children,
                            seps: right_seps,
                        }),
                    );
                    (
                        inserted,
                        Some(SplitUp {
                            sep: promote,
                            right: right_id,
                        }),
                    )
                } else {
                    (inserted, None)
                }
            }
        }
    }

    fn insert_leaf(
        &mut self,
        id: PageId,
        leaf: &mut LeafNode,
        key: i64,
        value: u64,
    ) -> (bool, Option<SplitUp>) {
        let pos = match leaf.locate(key) {
            Ok(pos) => pos,
            Err(pos) => {
                leaf.values[pos] = value; // 键已存在：覆盖
                store_node(&mut self.pager, id, &Node::Leaf(std::mem::take(leaf)));
                return (false, None);
            }
        };
        leaf.keys.insert(pos, key);
        leaf.values.insert(pos, value);
        let cap = Node::leaf_capacity(self.pager.page_size());
        if leaf.keys.len() <= cap {
            store_node(&mut self.pager, id, &Node::Leaf(std::mem::take(leaf)));
            return (true, None);
        }
        // 叶溢出：平分。左半复用本页，右半落到新页并接上叶链。
        let total = leaf.keys.len();
        let mid = total / 2;
        let right = LeafNode {
            keys: leaf.keys.drain(mid..).collect(),
            values: leaf.values.drain(mid..).collect(),
            next: leaf.next,
        };
        let sep = right.keys[0];
        let right_id = alloc_leaf(&mut self.pager);
        leaf.next = right_id;
        store_node(&mut self.pager, id, &Node::Leaf(std::mem::take(leaf)));
        store_node(&mut self.pager, right_id, &Node::Leaf(right));
        (
            true,
            Some(SplitUp {
                sep,
                right: right_id,
            }),
        )
    }

    // ---------------- 删除 ----------------

    /// 删除键。返回 true 表示删除成功，false 表示键不存在（树不变）。
    pub fn delete(&mut self, key: i64) -> bool {
        let root = self.pager.root_page();
        let res = self.delete_rec(root, key, true);
        res.found
    }

    fn delete_rec(&mut self, id: PageId, key: i64, is_root: bool) -> Del {
        let mut node = load_node(&self.pager, id);
        match &mut node {
            Node::Leaf(leaf) => {
                let pos = match leaf.locate(key) {
                    Ok(_) => {
                        return Del {
                            found: false,
                            underflow: false,
                        }
                    }
                    Err(p) => p,
                };
                leaf.keys.remove(pos);
                leaf.values.remove(pos);
                let underflow = !is_root && leaf.keys.len() < self.leaf_min();
                store_node(&mut self.pager, id, &node);
                Del {
                    found: true,
                    underflow,
                }
            }
            Node::Internal(internal) => {
                let p = internal.child_index(key);
                let child = internal.children[p];
                let res = self.delete_rec(child, key, false);
                if !res.found {
                    return Del {
                        found: false,
                        underflow: false,
                    };
                }
                if res.underflow {
                    self.rebalance_child(internal, p);
                }
                // 取出修复后所需信息，结束对 node 的可变借用。
                let child_count = internal.children.len();
                let only_child = if is_root && child_count == 1 {
                    Some(internal.children[0])
                } else {
                    None
                };
                // 根内部只剩一个孩子：塌缩一层。
                if let Some(only) = only_child {
                    self.pager.set_root_page(only);
                    self.pager.free_page(id); // 此时根已改，id 允许释放
                } else {
                    store_node(&mut self.pager, id, &node);
                }
                Del {
                    found: true,
                    underflow: !is_root && child_count < self.internal_min(),
                }
            }
        }
    }

    /// 修复内部节点 `parent` 中处于欠占用的孩子 `children[p]`：
    /// 优先向左右兄弟借位，否则与兄弟合并。会就地改写 `parent` 的孩子/分隔键，
    /// 并直接读写相关子页；调用方负责落盘 `parent` 页本身。
    fn rebalance_child(&mut self, parent: &mut InternalNode, p: usize) {
        // 优先向左兄弟借，其次向右兄弟借。
        if p > 0 {
            let left_id = parent.children[p - 1];
            let mut left = load_node(&self.pager, left_id);
            if self.can_lend(&left) {
                self.borrow_from_left(parent, p, &mut left, left_id);
                return;
            }
        }
        if p + 1 < parent.children.len() {
            let right_id = parent.children[p + 1];
            let mut right = load_node(&self.pager, right_id);
            if self.can_lend(&right) {
                self.borrow_from_right(parent, p, &mut right, right_id);
                return;
            }
        }
        // 无法借位：合并。优先并入左兄弟。
        if p > 0 {
            self.merge_with_left(parent, p);
        } else {
            self.merge_with_right(parent, p);
        }
    }

    /// 兄弟节点是否有富余（高于最低占用）可供借出一项。
    fn can_lend(&self, node: &Node) -> bool {
        match node {
            Node::Leaf(l) => l.keys.len() > self.leaf_min(),
            Node::Internal(i) => i.children.len() > self.internal_min(),
        }
    }

    fn borrow_from_left(
        &mut self,
        parent: &mut InternalNode,
        p: usize,
        left: &mut Node,
        left_id: PageId,
    ) {
        let child_id = parent.children[p];
        let mut child = load_node(&self.pager, child_id);
        match (&mut *left, &mut child) {
            (Node::Leaf(l), Node::Leaf(c)) => {
                let k = l.keys.pop().unwrap();
                let v = l.values.pop().unwrap();
                c.keys.insert(0, k);
                c.values.insert(0, v);
                parent.seps[p - 1] = k; // 新边界 = 孩子新首键
                l.next = child_id;
            }
            (Node::Internal(l), Node::Internal(c)) => {
                let promoted = l.seps.pop().unwrap(); // 原左节点最后分隔键，上提
                let moved_child = l.children.pop().unwrap();
                let down_sep = parent.seps[p - 1]; // 原父分隔键，下沉到孩子
                c.children.insert(0, moved_child);
                c.seps.insert(0, down_sep);
                parent.seps[p - 1] = promoted;
            }
            _ => panic!("借位时左右节点层级不一致"),
        }
        store_node(&mut self.pager, left_id, left);
        store_node(&mut self.pager, child_id, &child);
    }

    fn borrow_from_right(
        &mut self,
        parent: &mut InternalNode,
        p: usize,
        right: &mut Node,
        right_id: PageId,
    ) {
        let child_id = parent.children[p];
        let mut child = load_node(&self.pager, child_id);
        match (&mut child, &mut *right) {
            (Node::Leaf(c), Node::Leaf(r)) => {
                let k = r.keys.remove(0);
                let v = r.values.remove(0);
                c.keys.push(k);
                c.values.push(v);
                parent.seps[p] = r.keys[0]; // 新边界 = 右兄弟新首键
                c.next = right_id;
            }
            (Node::Internal(c), Node::Internal(r)) => {
                let moved_child = r.children.remove(0);
                let promoted = r.seps.remove(0); // 原右节点首分隔键，上提
                let down_sep = parent.seps[p]; // 原父分隔键，下沉到孩子
                c.children.push(moved_child);
                c.seps.push(down_sep);
                parent.seps[p] = promoted;
            }
            _ => panic!("借位时左右节点层级不一致"),
        }
        store_node(&mut self.pager, child_id, &child);
        store_node(&mut self.pager, right_id, right);
    }

    /// 把 children[p] 并入左兄弟 children[p-1]，随后从父节点删除孩子 p。
    fn merge_with_left(&mut self, parent: &mut InternalNode, p: usize) {
        let left_id = parent.children[p - 1];
        let child_id = parent.children[p];
        let down_sep = parent.seps[p - 1];
        let mut left = load_node(&self.pager, left_id);
        let child = load_node(&self.pager, child_id);
        match (&mut left, child) {
            (Node::Leaf(l), Node::Leaf(c)) => {
                l.keys.extend(c.keys);
                l.values.extend(c.values);
                l.next = c.next; // 维护叶链：跨过被吸收的页
            }
            (Node::Internal(l), Node::Internal(c)) => {
                l.seps.push(down_sep);
                l.seps.extend(c.seps);
                l.children.extend(c.children);
            }
            _ => panic!("合并时左右节点层级不一致"),
        }
        store_node(&mut self.pager, left_id, &left);
        parent.children.remove(p);
        parent.seps.remove(p - 1);
        self.pager.free_page(child_id);
    }

    /// 把右兄弟 children[p+1] 并入 children[p]，随后从父节点删除孩子 p+1。
    fn merge_with_right(&mut self, parent: &mut InternalNode, p: usize) {
        let child_id = parent.children[p];
        let right_id = parent.children[p + 1];
        let down_sep = parent.seps[p];
        let mut child = load_node(&self.pager, child_id);
        let right = load_node(&self.pager, right_id);
        match (&mut child, right) {
            (Node::Leaf(c), Node::Leaf(r)) => {
                c.keys.extend(r.keys);
                c.values.extend(r.values);
                c.next = r.next;
            }
            (Node::Internal(c), Node::Internal(r)) => {
                c.seps.push(down_sep);
                c.seps.extend(r.seps);
                c.children.extend(r.children);
            }
            _ => panic!("合并时左右节点层级不一致"),
        }
        store_node(&mut self.pager, child_id, &child);
        parent.children.remove(p + 1);
        parent.seps.remove(p);
        self.pager.free_page(right_id);
    }

    // ---------------- 全树校验 ----------------

    /// 递归/遍历全树，检查分隔键、最低/最高占用率与叶链一致性，返回统计信息。
    pub fn verify(&self) -> Result<TreeStats, String> {
        let ps = self.pager.page_size();
        let leaf_cap = Node::leaf_capacity(ps);
        let int_cap = Node::internal_capacity(ps);
        let leaf_min = Node::leaf_min_fill(ps);
        let int_min = Node::internal_min_children(ps);
        let root = self.pager.root_page();

        let mut occupancies = Vec::new();
        let mut dfs_leaves: Vec<PageId> = Vec::new();
        let mut leaf_count = 0usize;
        let mut internal_count = 0usize;
        let mut item_count = 0usize;
        let mut height = 0usize;

        // 返回该子树覆盖的 (最小键, 最大键)。递归 DFS 累加器参数较多，集中在此处。
        #[allow(clippy::too_many_arguments)]
        fn check<P: Pager>(
            tree: &BPTree<P>,
            id: PageId,
            is_root: bool,
            depth: usize,
            occ: &mut Vec<f64>,
            dfs_leaves: &mut Vec<PageId>,
            leaf_count: &mut usize,
            internal_count: &mut usize,
            item_count: &mut usize,
            height: &mut usize,
            leaf_cap: usize,
            int_cap: usize,
            leaf_min: usize,
            int_min: usize,
        ) -> Result<(i64, i64), String> {
            let node = load_node(&tree.pager, id);
            match node {
                Node::Leaf(l) => {
                    *height = (*height).max(depth + 1);
                    *leaf_count += 1;
                    dfs_leaves.push(id);
                    *item_count += l.keys.len();
                    occ.push(l.keys.len() as f64 / leaf_cap as f64);
                    if l.keys.len() > leaf_cap {
                        return Err(format!(
                            "叶页 {id} 项数 {} 超过容量 {leaf_cap}",
                            l.keys.len()
                        ));
                    }
                    if !is_root && l.keys.len() < leaf_min {
                        return Err(format!(
                            "非根叶页 {id} 项数 {} 低于最低占用 {leaf_min}",
                            l.keys.len()
                        ));
                    }
                    if is_root && !l.keys.is_empty() {
                        // 根为叶即树高 1，合法。
                    }
                    for w in l.keys.windows(2) {
                        if w[0] >= w[1] {
                            return Err(format!("叶页 {id} 键未严格升序: {} >= {}", w[0], w[1]));
                        }
                    }
                    l.keys
                        .first()
                        .map(|k| (*k, *l.keys.last().unwrap()))
                        .ok_or_else(|| {
                            if is_root {
                                // 空树根叶：用哨兵由上层特判处理不到，这里给个错误再特判。
                                "EMPTY_ROOT".to_string()
                            } else {
                                format!("非根叶页 {id} 为空")
                            }
                        })
                }
                Node::Internal(intern) => {
                    *internal_count += 1;
                    occ.push(intern.children.len() as f64 / int_cap as f64);
                    if intern.children.len() > int_cap {
                        return Err(format!(
                            "内部页 {id} 孩子数 {} 超过容量 {int_cap}",
                            intern.children.len()
                        ));
                    }
                    if is_root && intern.children.len() < 2 {
                        return Err(format!(
                            "根内部页 {id} 孩子数 {} 小于 2（应当塌缩而未塌缩）",
                            intern.children.len()
                        ));
                    }
                    if !is_root && intern.children.len() < int_min {
                        return Err(format!(
                            "非根内部页 {id} 孩子数 {} 低于最低 {int_min}",
                            intern.children.len()
                        ));
                    }
                    if intern.seps.len() + 1 != intern.children.len() {
                        return Err(format!("内部页 {id} 分隔键/孩子数目不匹配"));
                    }
                    for w in intern.seps.windows(2) {
                        if w[0] >= w[1] {
                            return Err(format!("内部页 {id} 分隔键未严格升序"));
                        }
                    }
                    let mut ranges = Vec::with_capacity(intern.children.len());
                    for (idx, &cid) in intern.children.iter().enumerate() {
                        let r = check(
                            tree,
                            cid,
                            false,
                            depth + 1,
                            occ,
                            dfs_leaves,
                            leaf_count,
                            internal_count,
                            item_count,
                            height,
                            leaf_cap,
                            int_cap,
                            leaf_min,
                            int_min,
                        )?;
                        ranges.push(r);
                        if idx > 0 {
                            let sep = intern.seps[idx - 1];
                            let (_, prev_max) = ranges[idx - 1];
                            let (cur_min, _) = ranges[idx];
                            if prev_max >= sep {
                                return Err(format!(
                                    "内部页 {id}: 左子树最大键 {prev_max} 应 < 分隔键 {sep}"
                                ));
                            }
                            if cur_min < sep {
                                return Err(format!(
                                    "内部页 {id}: 右子树最小键 {cur_min} 应 >= 分隔键 {sep}"
                                ));
                            }
                        }
                    }
                    Ok((ranges.first().unwrap().0, ranges.last().unwrap().1))
                }
            }
        }

        let root_node = load_node(&self.pager, root);
        let empty_root = matches!(&root_node, Node::Leaf(l) if is_empty_leaf(l));
        if empty_root {
            height = 1;
            leaf_count = 1;
            occupancies.push(0.0);
        } else {
            let r = check(
                self,
                root,
                true,
                0,
                &mut occupancies,
                &mut dfs_leaves,
                &mut leaf_count,
                &mut internal_count,
                &mut item_count,
                &mut height,
                leaf_cap,
                int_cap,
                leaf_min,
                int_min,
            );
            if let Err(e) = r {
                if e == "EMPTY_ROOT" {
                    // 空根叶：合法
                } else {
                    return Err(e);
                }
            }
        }

        // 校验叶链：从最左叶开始沿 next 走，顺序必须与中序遍历一致且末端为 NIL。
        if !dfs_leaves.is_empty() {
            // 最左叶 = 沿最左孩子一路下降
            let mut leftmost = root;
            loop {
                match load_node(&self.pager, leftmost) {
                    Node::Leaf(_) => break,
                    Node::Internal(i) => leftmost = i.children[0],
                }
            }
            let mut chain = Vec::new();
            let mut cur = leftmost;
            while cur != NIL {
                chain.push(cur);
                let l = load_node(&self.pager, cur).as_leaf().clone();
                cur = l.next;
                if chain.len() > leaf_count + 1 {
                    return Err("叶链出现环或超出叶数量".to_string());
                }
            }
            if chain != dfs_leaves {
                return Err(format!(
                    "叶链顺序与中序遍历不一致：chain={chain:?}, dfs={dfs_leaves:?}"
                ));
            }
            // 叶链相邻页的键必须严格递增衔接。
            for w in chain.windows(2) {
                let a = load_node(&self.pager, w[0]).as_leaf().clone();
                let b = load_node(&self.pager, w[1]).as_leaf().clone();
                match (a.keys.last(), b.keys.first()) {
                    (Some(&x), Some(&y)) => {
                        if x >= y {
                            return Err(format!("叶链衔接处键未递增: {x} >= {y}"));
                        }
                    }
                    _ => return Err("叶链中存在空叶".to_string()),
                }
            }
        }

        Ok(TreeStats {
            height,
            internal_pages: internal_count,
            leaf_pages: leaf_count,
            item_count,
            occupancies,
        })
    }

    pub fn sync(&mut self) -> std::io::Result<()> {
        self.pager.sync()
    }
}

fn is_empty_leaf(l: &LeafNode) -> bool {
    l.keys.is_empty() && l.values.is_empty() && l.next == NIL
}

/// 节点分裂后上推给父节点的信息。
struct SplitUp {
    sep: i64,
    right: PageId,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pager::MemoryPager;

    fn tree(ps: usize) -> BPTree<MemoryPager> {
        BPTree::new(MemoryPager::new(ps))
    }

    #[test]
    fn insert_get_basic() {
        let mut t = tree(64);
        assert!(t.insert(10, 100));
        assert!(t.insert(20, 200));
        assert_eq!(t.get(10), Some(100));
        assert_eq!(t.get(20), Some(200));
        assert_eq!(t.get(30), None);
        // 覆盖
        assert!(!t.insert(10, 111));
        assert_eq!(t.get(10), Some(111));
        t.verify().unwrap();
    }

    #[test]
    fn grows_multiple_levels_and_ranges() {
        let mut t = tree(64); // 叶容量 3，频繁分裂
        for k in 1..=50 {
            t.insert(k, k as u64 * 10);
            t.verify().unwrap();
        }
        let st = t.verify().unwrap();
        assert!(
            st.height >= 3,
            "50 个键在 64B 页下树高应 >= 3，实际 {}",
            st.height
        );
        for k in 1..=50 {
            assert_eq!(t.get(k), Some(k as u64 * 10));
        }
        let r: Vec<i64> = t.range(15, 19).iter().map(|(k, _)| *k).collect();
        assert_eq!(r, vec![15, 16, 17, 18, 19]);
        assert!(t.range(60, 100).is_empty());
        assert_eq!(t.range(0, 1), vec![(1, 10)]);
    }

    #[test]
    fn delete_shrinks_back_to_single_level() {
        let mut t = tree(64);
        for k in 1..=60 {
            t.insert(k, k as u64);
        }
        let grown = t.verify().unwrap();
        assert!(grown.height >= 3);
        for k in 1..=60 {
            assert!(t.delete(k), "删除 {k} 应成功");
            t.verify().unwrap();
            let st = t.verify().unwrap();
            assert_eq!(st.item_count, 60 - k as usize);
        }
        let st = t.verify().unwrap();
        assert_eq!(st.height, 1, "全部删除后应降回单层叶根，实际 {}", st.height);
        assert_eq!(st.leaf_pages, 1);
        assert_eq!(st.internal_pages, 0);
        assert!(t.range(i64::MIN, i64::MAX).is_empty());
    }

    #[test]
    fn delete_nonexistent_is_noop() {
        let mut t = tree(64);
        t.insert(1, 1);
        t.insert(2, 2);
        assert!(!t.delete(999));
        t.verify().unwrap();
        assert_eq!(t.get(1), Some(1));
        assert_eq!(t.get(2), Some(2));
    }

    #[test]
    fn negative_and_boundary_keys() {
        let mut t = tree(80);
        let keys = [i64::MIN, -100, -1, 0, 1, 100, i64::MAX];
        for k in keys {
            t.insert(k, k as u64);
            t.verify().unwrap();
        }
        for k in keys {
            assert_eq!(t.get(k), Some(k as u64));
        }
        let r: Vec<i64> = t
            .range(i64::MIN, i64::MAX)
            .iter()
            .map(|(k, _)| *k)
            .collect();
        assert_eq!(r, keys.to_vec());
        for k in keys {
            t.delete(k);
            t.verify().unwrap();
        }
    }
}
