//! 依赖图处理：校验、环定位（迭代式 DFS）、拓扑排序（Kahn）。

use std::collections::{HashMap, VecDeque};

use crate::model::{BuildGraph, EdgeDto, NodeKind, Rule};

/// 准备好的图布局：拓扑顺序 + 便于查询的索引。
#[derive(Debug)]
pub struct Layout {
    /// 全部节点的拓扑顺序（输入与目标混合，边方向：依赖在前）
    pub order: Vec<String>,
    pub kinds: HashMap<String, NodeKind>,
    pub deps: HashMap<String, Vec<String>>,
    pub rules: HashMap<String, Rule>,
}

/// 图相关错误。
#[derive(Debug, PartialEq, Eq)]
pub enum GraphError {
    /// 请求层面的校验错误（400）
    Invalid(Vec<String>),
    /// 存在环（422）
    Cycle(Cycle),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Cycle {
    pub nodes: Vec<String>,
    pub edges: Vec<EdgeDto>,
}

/// 校验图并计算拓扑顺序。
pub fn prepare(graph: &BuildGraph) -> Result<Layout, GraphError> {
    let mut errs: Vec<String> = Vec::new();

    let mut kinds: HashMap<String, NodeKind> = HashMap::new();
    let mut deps: HashMap<String, Vec<String>> = HashMap::new();
    let mut rules: HashMap<String, Rule> = HashMap::new();

    // 1. 结构校验：空 id、重复 id、input 不许有规则/依赖。
    for n in &graph.nodes {
        if n.id.trim().is_empty() {
            errs.push("graph contains a node with an empty id".to_string());
            continue;
        }
        if kinds.contains_key(&n.id) {
            errs.push(format!("duplicate node id in graph: '{}'", n.id));
            continue;
        }
        kinds.insert(n.id.clone(), n.kind);
        deps.insert(n.id.clone(), n.depends_on.clone());

        match n.kind {
            NodeKind::Input => {
                if n.rule.is_some() {
                    errs.push(format!("input node '{}' must not declare a rule", n.id));
                }
                if !n.depends_on.is_empty() {
                    errs.push(format!("input node '{}' must not declare dependencies", n.id));
                }
            }
            NodeKind::Target => {
                if let Some(r) = &n.rule {
                    rules.insert(n.id.clone(), r.clone());
                } else {
                    errs.push(format!("target node '{}' is missing a rule", n.id));
                }
            }
        }
    }

    // 2. 依赖必须指向已声明节点。
    for (id, ds) in &deps {
        for d in ds {
            if !kinds.contains_key(d) {
                errs.push(format!("node '{id}' depends on unknown node '{d}'"));
            }
        }
    }

    if !errs.is_empty() {
        return Err(GraphError::Invalid(errs));
    }

    // 3. 环检测（含自环）。
    if let Some(cycle) = find_cycle(&kinds, &deps) {
        return Err(GraphError::Cycle(cycle));
    }

    // 4. Kahn 拓扑排序；id 排序保证结果确定。
    let mut indeg: HashMap<&str, usize> = kinds.keys().map(|id| (id.as_str(), 0usize)).collect();
    let mut dependents: HashMap<&str, Vec<&str>> = HashMap::new();
    for (id, ds) in &deps {
        indeg.insert(id.as_str(), ds.len());
        for d in ds {
            dependents.entry(d.as_str()).or_default().push(id.as_str());
        }
    }

    let mut ready: VecDeque<&str> = indeg
        .iter()
        .filter_map(|(id, n)| (*n == 0).then_some(*id))
        .collect();
    // 确定性：每轮按 id 排序后取最小。
    let mut order: Vec<String> = Vec::with_capacity(kinds.len());
    while let Some(id) = pop_min(&mut ready) {
        order.push(id.to_string());
        if let Some(children) = dependents.get(id) {
            for c in children {
                let v = indeg.get_mut(*c).expect("indegree present");
                *v -= 1;
                if *v == 0 {
                    ready.push_back(c);
                }
            }
        }
    }

    // 上面已做环检测，这里仅作防御。
    if order.len() != kinds.len() {
        return Err(GraphError::Cycle(
            find_cycle(&kinds, &deps).expect("a cycle must exist when topo sort stalls"),
        ));
    }

    Ok(Layout { order, kinds, deps, rules })
}

fn pop_min<'a>(q: &mut VecDeque<&'a str>) -> Option<&'a str> {
    if q.is_empty() {
        return None;
    }
    let mut pos = 0usize;
    for i in 1..q.len() {
        if q[i] < q[pos] {
            pos = i;
        }
    }
    q.swap_remove_back(pos)
}

/// 迭代式三色 DFS 找一条环。
///
/// 返回的 `nodes` 首尾同 id（如 `[a,b,c,a]`），`edges` 为环上的有向边。
fn find_cycle(kinds: &HashMap<String, NodeKind>, deps: &HashMap<String, Vec<String>>) -> Option<Cycle> {
    const GRAY: u8 = 1;
    const BLACK: u8 = 2;
    let mut color: HashMap<&str, u8> = HashMap::new();

    let mut roots: Vec<&String> = kinds.keys().collect();
    roots.sort();

    // 栈帧：(节点, 下一个待访问的依赖下标)
    let mut stack: Vec<(&str, usize)> = Vec::new();
    // 当前 DFS 路径上的节点，便于提取环
    let mut path: Vec<&str> = Vec::new();

    for root in roots {
        if color.contains_key(root.as_str()) {
            continue;
        }
        color.insert(root.as_str(), GRAY);
        stack.push((root.as_str(), 0));
        path.push(root.as_str());

        while let Some((node, idx)) = stack.last_mut() {
            let ds = deps.get(*node).map(Vec::as_slice).unwrap_or(&[]);
            if *idx >= ds.len() {
                color.insert(*node, BLACK);
                stack.pop();
                path.pop();
                continue;
            }
            let dep = ds[*idx].as_str();
            *idx += 1;

            match color.get(dep).copied() {
                Some(GRAY) => {
                    // 从 path 中 dep 的位置截取环
                    let start = path.iter().position(|p| *p == dep).expect("gray node is on path");
                    let cyc_nodes: Vec<String> =
                        path[start..].iter().map(|s| s.to_string()).collect();
                    let mut nodes = cyc_nodes.clone();
                    nodes.push(dep.to_string());
                    let edges = cyc_nodes
                        .iter()
                        .zip(nodes[1..].iter())
                        .map(|(from, to)| EdgeDto { from: from.clone(), to: to.clone() })
                        .collect();
                    return Some(Cycle { nodes, edges });
                }
                Some(BLACK) => {}
                _ => {
                    color.insert(dep, GRAY);
                    stack.push((dep, 0));
                    path.push(dep);
                }
            }
        }
    }
    None
}

