//! 磁盘 B+ 树本体：插入（含根分裂）、删除（借位 / 合并 / 根坍缩降高）、
//! 点查与有序范围读，以及叶链维护。
//!
//! 递归都在“读入内存的节点副本”上进行，回溯时才把改动页写回磁盘；
//! 一次操作结束后统一写元数据页，使根指针 / 计数的切换是单点落盘。

use std::io;
use std::path::Path;

use crate::node::{Internal, Key, Leaf, Limits, Node, PageId, Value, NULL_PAGE};
use crate::pager::{Meta, Pager};

/// 插入结果。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PutOutcome {
    /// 新插入了键。
    Inserted,
    /// 覆盖了已有键的旧值（附带旧值）。
    Replaced(Value),
}

/// 删除结果。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DelOutcome {
    Deleted,
    NotFound,
}

/// 磁盘 B+ 树句柄。
pub struct BpTree {
    pager: Pager,
    limits: Limits,
    root: PageId,
    leftmost: PageId,
    key_count: u64,
    buf: Vec<u8>,
}

/// 节点向上分裂的产物。
struct SplitUp {
    /// 分隔键。叶分裂时等于新右叶最小键；内部分裂时是被上推的中间键。
    sep: Key,
    right: PageId,
}

impl BpTree {
    /// 创建新数据库文件。
    pub fn create<P: AsRef<Path>>(path: P, page_size: u16) -> io::Result<Self> {
        Self::create_opts(path, page_size, true)
    }

    /// 创建新数据库文件，可关闭逐操作 fsync（仅供测试加速）。
    pub fn create_opts<P: AsRef<Path>>(path: P, page_size: u16, sync: bool) -> io::Result<Self> {
        let mut pager = Pager::create(path, page_size)?;
        if !sync {
            pager.set_sync(false);
        }
        let limits = Limits::for_page_size(page_size);
        if limits.leaf_max < 3 || limits.internal_max < 3 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "页大小过小，至少需要容纳 3 个叶键、3 个内部分隔键",
            ));
        }
        Ok(BpTree {
            pager,
            limits,
            root: NULL_PAGE,
            leftmost: NULL_PAGE,
            key_count: 0,
            buf: vec![0u8; page_size as usize],
        })
    }

    /// 打开已有数据库文件（每次写后 fsync）。
    pub fn open<P: AsRef<Path>>(path: P) -> io::Result<Self> {
        Self::open_opts(path, true)
    }

    /// 打开数据库文件，`sync = false` 可关闭逐操作 fsync（仅供测试加速）。
    pub fn open_opts<P: AsRef<Path>>(path: P, sync: bool) -> io::Result<Self> {
        let mut pager = Pager::open(path, sync)?;
        let meta = pager.read_meta()?;
        let limits = Limits::for_page_size(meta.page_size);
        Ok(BpTree {
            pager,
            limits,
            root: meta.root,
            leftmost: meta.leftmost,
            key_count: meta.key_count,
            buf: vec![0u8; meta.page_size as usize],
        })
    }

    pub fn page_size(&self) -> u16 {
        self.pager.page_size()
    }

    pub fn limits(&self) -> Limits {
        self.limits
    }

    pub fn key_count(&self) -> u64 {
        self.key_count
    }

    pub fn root_id(&self) -> PageId {
        self.root
    }

    pub fn leftmost_id(&self) -> PageId {
        self.leftmost
    }

    /// 元数据快照（供 /stats 使用）。
    pub fn meta_snapshot(&self) -> Meta {
        Meta {
            root: self.root,
            leftmost: self.leftmost,
            page_size: self.pager.page_size(),
            free_head: self.pager.free_head(),
            key_count: self.key_count,
        }
    }

    // ---- 底层读写辅助 ----

    pub(crate) fn load(&mut self, id: PageId) -> io::Result<Node> {
        self.pager.read_page(id, &mut self.buf)?;
        Node::decode(&self.buf)
    }

    fn store(&mut self, id: PageId, node: &Node) -> io::Result<()> {
        node.encode(&mut self.buf)?;
        self.pager.write_page(id, &self.buf)
    }

    fn save_meta(&mut self) -> io::Result<()> {
        self.pager.write_meta(&Meta {
            root: self.root,
            leftmost: self.leftmost,
            page_size: self.pager.page_size(),
            free_head: self.pager.free_head(),
            key_count: self.key_count,
        })
    }

    // ---- 点查 ----

    /// 按键查值。
    pub fn get(&mut self, key: Key) -> io::Result<Option<Value>> {
        if self.root == NULL_PAGE {
            return Ok(None);
        }
        let mut id = self.root;
        loop {
            match self.load(id)? {
                Node::Leaf(leaf) => match leaf.position(key) {
                    Ok(i) => return Ok(Some(leaf.values[i])),
                    Err(_) => return Ok(None),
                },
                Node::Internal(internal) => {
                    id = internal.children[internal.child_index(key)];
                }
            }
        }
    }

    // ---- 插入 ----

    /// 插入或覆盖；返回新插入还是替换旧值。
    pub fn put(&mut self, key: Key, value: Value) -> io::Result<PutOutcome> {
        if self.root == NULL_PAGE {
            // 空树：分配单叶根。
            let id = self.pager.alloc_page()?;
            let leaf = Leaf {
                keys: vec![key],
                values: vec![value],
                prev: NULL_PAGE,
                next: NULL_PAGE,
            };
            self.store(id, &Node::Leaf(leaf))?;
            self.root = id;
            self.leftmost = id;
            self.key_count = 1;
            self.save_meta()?;
            return Ok(PutOutcome::Inserted);
        }

        let (outcome, split) = self.insert_rec(self.root, key, value)?;
        if let Some(up) = split {
            // 根分裂：新根 = [旧根 | sep | 新右孩子]，树高 +1。
            let new_root = self.pager.alloc_page()?;
            self.store(
                new_root,
                &Node::Internal(Internal {
                    separators: vec![up.sep],
                    children: vec![self.root, up.right],
                }),
            )?;
            self.root = new_root;
        }
        if matches!(outcome, PutOutcome::Inserted) {
            self.key_count += 1;
        }
        self.save_meta()?;
        Ok(outcome)
    }

    /// 递归插入；返回（结果, 若发生分裂则带上推信息）。
    fn insert_rec(
        &mut self,
        id: PageId,
        key: Key,
        value: Value,
    ) -> io::Result<(PutOutcome, Option<SplitUp>)> {
        match self.load(id)? {
            Node::Leaf(mut leaf) => match leaf.position(key) {
                Ok(i) => {
                    let old = leaf.values[i];
                    leaf.values[i] = value;
                    self.store(id, &Node::Leaf(leaf))?;
                    Ok((PutOutcome::Replaced(old), None))
                }
                Err(pos) => {
                    leaf.keys.insert(pos, key);
                    leaf.values.insert(pos, value);
                    if leaf.keys.len() <= self.limits.leaf_max {
                        self.store(id, &Node::Leaf(leaf))?;
                        return Ok((PutOutcome::Inserted, None));
                    }
                    // 叶溢出（L = leaf_max+1 项）：左 ceil(L/2)、右 floor(L/2)。
                    let right_id = self.pager.alloc_page()?;
                    let split_at = leaf.keys.len().div_ceil(2);
                    let old_next = leaf.next;
                    let right = Leaf {
                        keys: leaf.keys.split_off(split_at),
                        values: leaf.values.split_off(split_at),
                        prev: id,
                        next: old_next,
                    };
                    let sep = right.keys[0];
                    leaf.next = right_id;
                    self.store(right_id, &Node::Leaf(right))?;
                    if old_next != NULL_PAGE {
                        if let Node::Leaf(mut next_leaf) = self.load(old_next)? {
                            next_leaf.prev = right_id;
                            self.store(old_next, &Node::Leaf(next_leaf))?;
                        } else {
                            return Err(io::Error::new(
                                io::ErrorKind::InvalidData,
                                "叶链后继不是叶节点",
                            ));
                        }
                    }
                    self.store(id, &Node::Leaf(leaf))?;
                    Ok((PutOutcome::Inserted, Some(SplitUp { sep, right: right_id })))
                }
            },
            Node::Internal(mut internal) => {
                let idx = internal.child_index(key);
                let child = internal.children[idx];
                let (outcome, up) = self.insert_rec(child, key, value)?;
                match up {
                    None => Ok((outcome, None)), // 本层内容不变，无需回写
                    Some(split) => {
                        internal.insert_split(idx, split.sep, split.right);
                        if internal.separators.len() <= self.limits.internal_max {
                            self.store(id, &Node::Internal(internal))?;
                            return Ok((outcome, None));
                        }
                        // 内部溢出（2t 个分隔键、2t+1 个孩子）：
                        // 上推第 t 个（下标 m=t），左留 t 键 t+1 孩子，右留 t-1 键 t 孩子。
                        let right_id = self.pager.alloc_page()?;
                        let m = internal.separators.len() / 2;
                        let up_sep = internal.separators[m];
                        let right_seps = internal.separators.split_off(m + 1);
                        internal.separators.pop(); // 去掉上推键
                        let right_children = internal.children.split_off(m + 1);
                        self.store(
                            right_id,
                            &Node::Internal(Internal {
                                separators: right_seps,
                                children: right_children,
                            }),
                        )?;
                        self.store(id, &Node::Internal(internal))?;
                        Ok((outcome, Some(SplitUp { sep: up_sep, right: right_id })))
                    }
                }
            }
        }
    }

    // ---- 删除 ----

    /// 删除键；不存在返回 [`DelOutcome::NotFound`]。
    pub fn delete(&mut self, key: Key) -> io::Result<DelOutcome> {
        if self.root == NULL_PAGE {
            return Ok(DelOutcome::NotFound);
        }
        let outcome = self.delete_rec(self.root, key)?;
        if matches!(outcome, DelOutcome::Deleted) {
            // 回溯结束后处理根：
            // - 内部根只剩 1 个孩子 → 坍缩降高（可能连续多级）
            // - 叶根为空 → 回到空树
            loop {
                let collapse = match self.load(self.root)? {
                    Node::Internal(internal) => internal.children.len() == 1,
                    Node::Leaf(leaf) => leaf.keys.is_empty(),
                };
                if !collapse {
                    break;
                }
                let node = self.load(self.root)?;
                match node {
                    Node::Internal(internal) => {
                        let only = internal.children[0];
                        self.pager.free_page(self.root)?;
                        self.root = only;
                    }
                    Node::Leaf(leaf) => {
                        debug_assert_eq!(leaf.prev, NULL_PAGE);
                        debug_assert_eq!(leaf.next, NULL_PAGE);
                        self.pager.free_page(self.root)?;
                        self.root = NULL_PAGE;
                        self.leftmost = NULL_PAGE;
                        break;
                    }
                }
            }
            self.key_count -= 1;
            self.save_meta()?;
        }
        Ok(outcome)
    }

    /// 递归删除；回溯时修复下溢子节点。
    fn delete_rec(&mut self, id: PageId, key: Key) -> io::Result<DelOutcome> {
        match self.load(id)? {
            Node::Leaf(mut leaf) => match leaf.position(key) {
                Err(_) => Ok(DelOutcome::NotFound),
                Ok(i) => {
                    leaf.keys.remove(i);
                    leaf.values.remove(i);
                    self.store(id, &Node::Leaf(leaf))?;
                    Ok(DelOutcome::Deleted)
                }
            },
            Node::Internal(mut internal) => {
                let idx = internal.child_index(key);
                let child_id = internal.children[idx];
                let outcome = self.delete_rec(child_id, key)?;
                if matches!(outcome, DelOutcome::Deleted) {
                    // 重读孩子，检查下溢；下溢则借位或合并。
                    let child = self.load(child_id)?;
                    let underflow = match &child {
                        Node::Leaf(l) => l.keys.len() < self.limits.leaf_min(),
                        Node::Internal(i) => {
                            i.separators.len() < self.limits.internal_min_seps()
                        }
                    };
                    if underflow {
                        self.fix_child(&mut internal, idx, child)?;
                    }
                    self.store(id, &Node::Internal(internal))?;
                }
                Ok(outcome)
            }
        }
    }

    /// 修复下溢孩子 `children[idx]`（节点已读入为 `child`）：
    /// 依次尝试向左兄弟借、向右兄弟借、与左/右兄弟合并。
    /// 合并会从父节点移除孩子与分隔键，并释放被合并掉的页。
    fn fix_child(
        &mut self,
        parent: &mut Internal,
        idx: usize,
        child: Node,
    ) -> io::Result<()> {
        // 左兄弟借得出？
        if idx > 0 {
            let left_id = parent.children[idx - 1];
            let left = self.load(left_id)?;
            if can_spare(&left, self.limits) {
                self.borrow_from_left(parent, idx, left_id, left, child)?;
                return Ok(());
            }
        }
        // 右兄弟借得出？
        if idx + 1 < parent.children.len() {
            let right_id = parent.children[idx + 1];
            let right = self.load(right_id)?;
            if can_spare(&right, self.limits) {
                self.borrow_from_right(parent, idx, right_id, right, child)?;
                return Ok(());
            }
        }
        // 都借不出：合并。优先并入左兄弟。
        if idx > 0 {
            let left_id = parent.children[idx - 1];
            let left = self.load(left_id)?;
            let right_id = parent.children[idx];
            self.merge(parent, idx - 1, left_id, left, right_id, child)?;
        } else {
            let right_id = parent.children[1];
            let right = self.load(right_id)?;
            let left_id = parent.children[0];
            self.merge(parent, 0, left_id, child, right_id, right)?;
        }
        Ok(())
    }

    /// 向左兄弟末尾借一项。
    fn borrow_from_left(
        &mut self,
        parent: &mut Internal,
        idx: usize,
        left_id: PageId,
        mut left: Node,
        mut child: Node,
    ) -> io::Result<()> {
        let sep_idx = idx - 1;
        match (&mut left, &mut child) {
            (Node::Leaf(l), Node::Leaf(c)) => {
                let k = l.keys.pop().unwrap();
                let v = l.values.pop().unwrap();
                c.keys.insert(0, k);
                c.values.insert(0, v);
                parent.separators[sep_idx] = c.keys[0];
            }
            (Node::Internal(l), Node::Internal(c)) => {
                let pulled_sep = l.separators.pop().unwrap();
                let moved_child = l.children.pop().unwrap();
                let old_sep = parent.separators[sep_idx];
                c.children.insert(0, moved_child);
                c.separators.insert(0, old_sep);
                parent.separators[sep_idx] = pulled_sep;
            }
            _ => unreachable!("兄弟节点类型必须一致"),
        }
        self.store(left_id, &left)?;
        self.store(parent.children[idx], &child)?;
        Ok(())
    }

    /// 向右兄弟开头借一项。
    fn borrow_from_right(
        &mut self,
        parent: &mut Internal,
        idx: usize,
        right_id: PageId,
        mut right: Node,
        mut child: Node,
    ) -> io::Result<()> {
        let sep_idx = idx;
        match (&mut child, &mut right) {
            (Node::Leaf(c), Node::Leaf(r)) => {
                let k = r.keys.remove(0);
                let v = r.values.remove(0);
                c.keys.push(k);
                c.values.push(v);
                parent.separators[sep_idx] = r.keys[0];
            }
            (Node::Internal(c), Node::Internal(r)) => {
                let pulled_sep = r.separators.remove(0);
                let moved_child = r.children.remove(0);
                let old_sep = parent.separators[sep_idx];
                c.separators.push(old_sep);
                c.children.push(moved_child);
                parent.separators[sep_idx] = pulled_sep;
            }
            _ => unreachable!("兄弟节点类型必须一致"),
        }
        self.store(right_id, &right)?;
        self.store(parent.children[idx], &child)?;
        Ok(())
    }

    /// 合并左节点与右节点，中间夹父分隔键 `parent.separators[sep_idx]`，
    /// 结果留在左节点，右节点页释放，并从父节点移除右孩子与该分隔键。
    fn merge(
        &mut self,
        parent: &mut Internal,
        sep_idx: usize,
        left_id: PageId,
        mut left: Node,
        right_id: PageId,
        mut right: Node,
    ) -> io::Result<()> {
        let sep = parent.separators[sep_idx];
        match (&mut left, &mut right) {
            (Node::Leaf(l), Node::Leaf(r)) => {
                // 叶链维护：跨过被吸收的右叶。
                let after = r.next;
                l.keys.append(&mut r.keys);
                l.values.append(&mut r.values);
                l.next = after;
                if after != NULL_PAGE {
                    if let Node::Leaf(mut after_leaf) = self.load(after)? {
                        after_leaf.prev = left_id;
                        self.store(after, &Node::Leaf(after_leaf))?;
                    } else {
                        return Err(io::Error::new(
                            io::ErrorKind::InvalidData,
                            "叶链后继不是叶节点",
                        ));
                    }
                }
            }
            (Node::Internal(l), Node::Internal(r)) => {
                l.separators.push(sep);
                l.separators.append(&mut r.separators);
                l.children.append(&mut r.children);
            }
            _ => unreachable!("兄弟节点类型必须一致"),
        }
        self.store(left_id, &left)?;
        self.pager.free_page(right_id)?;
        parent.separators.remove(sep_idx);
        let removed = parent.children.remove(sep_idx + 1);
        debug_assert_eq!(removed, right_id);
        Ok(())
    }

    // ---- 范围读 ----

    /// 闭区间 `[start, end]` 有序扫描；`None` 表示无界。start > end 返回空。
    pub fn range(
        &mut self,
        start: Option<Key>,
        end: Option<Key>,
    ) -> io::Result<Vec<(Key, Value)>> {
        if let (Some(a), Some(b)) = (start, end) {
            if a > b {
                return Ok(Vec::new());
            }
        }
        if self.root == NULL_PAGE {
            return Ok(Vec::new());
        }
        // 定位起始叶：有下界沿树下探到对应叶，否则从最左叶开始。
        let mut leaf_id = match start {
            Some(k) => {
                let mut id = self.root;
                loop {
                    match self.load(id)? {
                        Node::Leaf(_) => break id,
                        Node::Internal(internal) => {
                            id = internal.children[internal.child_index(k)];
                        }
                    }
                }
            }
            None => self.leftmost,
        };

        let mut out = Vec::new();
        loop {
            let leaf = match self.load(leaf_id)? {
                Node::Leaf(l) => l,
                Node::Internal(_) => {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "叶链上出现内部节点（树结构损坏）",
                    ))
                }
            };
            for (i, &k) in leaf.keys.iter().enumerate() {
                if start.is_some_and(|s| k < s) {
                    continue;
                }
                if end.is_some_and(|e| k > e) {
                    return Ok(out);
                }
                out.push((k, leaf.values[i]));
            }
            match leaf.next {
                NULL_PAGE => break,
                next => leaf_id = next,
            }
        }
        Ok(out)
    }
}

/// 兄弟节点是否借得出一项（借出后仍不少于半满下限）。
fn can_spare(node: &Node, limits: Limits) -> bool {
    match node {
        Node::Leaf(l) => l.keys.len() > limits.leaf_min(),
        Node::Internal(i) => i.separators.len() > limits.internal_min_seps(),
    }
}
