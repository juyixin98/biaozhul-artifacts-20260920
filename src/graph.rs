//! 依赖图：节点 / 边的模型、校验、环定位与拓扑排序。
//!
//! 节点分两类：
//! - [`NodeKind::File`]：输入文件或构建产物，本身不“执行”；
//! - [`NodeKind::Build`]：一个构建动作（规则），声明 command、tool、tool_version、env，
//!   其缓存键由这些声明决定（见 [`crate::engine::build_cache_key`]）。
//!
//! 边 `u -> v` 表示数据/构建顺序依赖：v 的构建需要先完成 u。

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use serde::{Deserialize, Serialize};

/// 构建动作的规则声明。
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Rule {
    /// 执行命令，例如 `cc -c in.c -o out.o`。仅作声明，服务不会真正执行。
    pub command: String,
    /// 工具名，例如 `cc`、`protoc`、`webpack`。
    #[serde(default)]
    pub tool: String,
    /// 工具版本。工具版本变化会导致缓存键变化（相当于规则变化）。
    #[serde(default)]
    pub tool_version: String,
    /// 声明的环境（键值对）。环境变化会导致缓存键变化。
    /// 用 BTreeMap 保证与输入顺序无关的稳定序列化。
    #[serde(default)]
    pub env: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum NodeKind {
    File,
    Build,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Node {
    pub id: String,
    pub kind: NodeKind,
    /// 仅 build 节点使用；file 节点必须为 None。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rule: Option<Rule>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Edge {
    pub from: String,
    pub to: String,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Graph {
    pub nodes: Vec<Node>,
    #[serde(default)]
    pub edges: Vec<Edge>,
}

/// 一个有向环：按顺序排列的节点 id（首尾相接），以及构成该环的边。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Cycle {
    /// 环上的节点，按依赖方向排列；末尾节点有一条边指回 `nodes[0]`。
    pub nodes: Vec<String>,
    /// 环上的边（`from -> to`），长度与 `nodes` 相同。
    pub edges: Vec<(String, String)>,
}

impl Graph {
    /// 构建内部索引并做静态校验：
    /// - id 非空且唯一；
    /// - file 节点不能带 rule，build 节点必须带 rule；
    /// - 边引用的节点必须存在；自环即环；
    /// - 重复边被合并（菱形 DAG 中两条路径共享的节点/边只算一次）。
    pub fn compile(self) -> Result<CompiledGraph, Vec<GraphIssue>> {
        let mut issues = Vec::new();
        let mut kinds: HashMap<String, NodeKind> = HashMap::new();
        let mut rules: HashMap<String, Rule> = HashMap::new();
        let mut order: Vec<String> = Vec::new();

        for n in &self.nodes {
            if n.id.is_empty() {
                issues.push(GraphIssue {
                    code: "empty_node_id".to_string(),
                    message: "node id must not be empty".to_string(),
                    node_id: None,
                    edge: None,
                });
                continue;
            }
            if kinds.contains_key(&n.id) {
                issues.push(GraphIssue {
                    code: "duplicate_node".to_string(),
                    message: format!("duplicate node id: {}", n.id),
                    node_id: Some(n.id.clone()),
                    edge: None,
                });
                continue;
            }
            match n.kind {
                NodeKind::File if n.rule.is_some() => issues.push(GraphIssue {
                    code: "rule_on_file".to_string(),
                    message: format!("file node {} must not declare a rule", n.id),
                    node_id: Some(n.id.clone()),
                    edge: None,
                }),
                NodeKind::Build if n.rule.is_none() => issues.push(GraphIssue {
                    code: "missing_rule".to_string(),
                    message: format!("build node {} must declare a rule", n.id),
                    node_id: Some(n.id.clone()),
                    edge: None,
                }),
                _ => {}
            }
            kinds.insert(n.id.clone(), n.kind);
            if let Some(r) = &n.rule {
                rules.insert(n.id.clone(), r.clone());
            }
            order.push(n.id.clone());
        }

        let mut dedup: HashSet<(String, String)> = HashSet::new();
        let mut adj: HashMap<String, BTreeSet<String>> = HashMap::new();
        let mut indeg: HashMap<String, u32> = HashMap::new();
        for id in &order {
            adj.insert(id.clone(), BTreeSet::new());
            indeg.insert(id.clone(), 0);
        }

        for e in &self.edges {
            if !kinds.contains_key(&e.from) || !kinds.contains_key(&e.to) {
                issues.push(GraphIssue {
                    code: "unknown_edge_endpoint".to_string(),
                    message: format!("edge {} -> {} references an unknown node", e.from, e.to),
                    node_id: None,
                    edge: Some((e.from.clone(), e.to.clone())),
                });
                continue;
            }
            if dedup.insert((e.from.clone(), e.to.clone())) {
                adj.get_mut(&e.from).unwrap().insert(e.to.clone());
                *indeg.get_mut(&e.to).unwrap() += 1;
            }
        }

        if !issues.is_empty() {
            return Err(issues);
        }

        Ok(CompiledGraph {
            order,
            kinds,
            rules,
            adj,
            indeg,
        })
    }
}

/// 静态校验问题（HTTP 422 的结构化细节）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GraphIssue {
    pub code: String,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub node_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub edge: Option<(String, String)>,
}

#[derive(Debug, Clone)]
pub struct CompiledGraph {
    /// 节点的输入声明顺序（仅用于输出的稳定排序兜底）。
    pub order: Vec<String>,
    pub kinds: HashMap<String, NodeKind>,
    pub rules: HashMap<String, Rule>,
    /// 邻接表：后继使用 BTreeSet 得到确定性的 id 字典序。
    pub adj: HashMap<String, BTreeSet<String>>,
    indeg: HashMap<String, u32>,
}

impl CompiledGraph {
    pub fn node_ids(&self) -> impl Iterator<Item = &String> {
        self.order.iter()
    }

    pub fn is_file(&self, id: &str) -> bool {
        matches!(self.kinds.get(id), Some(NodeKind::File))
    }

    pub fn is_build(&self, id: &str) -> bool {
        matches!(self.kinds.get(id), Some(NodeKind::Build))
    }

    /// Kahn 拓扑排序。返回：
    /// - 可排序的节点（图无环时为全部节点），同层按 id 字典序，保证结果确定；
    /// - 剩余无法入队的节点（有环时即环上/被环挡住的节点）。
    pub fn topo_sort(&self) -> (Vec<String>, Vec<String>) {
        let mut indeg = self.indeg.clone();
        // 用 BTreeSet 当零入度队列：天然按 id 字典序出队。
        let mut ready: BTreeSet<String> = indeg
            .iter()
            .filter(|(_, &d)| d == 0)
            .map(|(id, _)| id.clone())
            .collect();
        let mut sorted = Vec::with_capacity(self.order.len());

        while let Some(id) = ready.pop_first() {
            for next in &self.adj[&id] {
                let d = indeg.get_mut(next).unwrap();
                *d -= 1;
                if *d == 0 {
                    ready.insert(next.clone());
                }
            }
            sorted.push(id);
        }

        if sorted.len() == self.order.len() {
            return (sorted, Vec::new());
        }
        let done: HashSet<&str> = sorted.iter().map(String::as_str).collect();
        let remaining = self
            .order
            .iter()
            .filter(|id| !done.contains(id.as_str()))
            .cloned()
            .collect();
        (sorted, remaining)
    }

    /// 环定位：基于 Tarjan 强连通分量。
    ///
    /// - 大小 >= 2 的 SCC 中存在环；
    /// - 大小为 1 但存在自环的 SCC 也是环。
    ///
    /// 对每个成环 SCC，用 DFS 在其内部找出一条具体的有向环返回，
    /// 同时给出构成该环的边，方便调用方定位。
    pub fn find_cycles(&self) -> Vec<Cycle> {
        self.tarjan_sccs()
            .into_iter()
            .filter_map(|scc| self.cycle_in_scc(&scc))
            .collect()
    }

    fn tarjan_sccs(&self) -> Vec<Vec<String>> {
        struct State {
            index: i64,
            indices: HashMap<String, i64>,
            low: HashMap<String, i64>,
            on_stack: HashSet<String>,
            stack: Vec<String>,
            sccs: Vec<Vec<String>>,
        }
        let mut st = State {
            index: 0,
            indices: HashMap::new(),
            low: HashMap::new(),
            on_stack: HashSet::new(),
            stack: Vec::new(),
            sccs: Vec::new(),
        };

        fn strong_connect(g: &CompiledGraph, v: &str, st: &mut State) {
            st.indices.insert(v.to_string(), st.index);
            st.low.insert(v.to_string(), st.index);
            st.index += 1;
            st.stack.push(v.to_string());
            st.on_stack.insert(v.to_string());

            for w in &g.adj[v] {
                if !st.indices.contains_key(w) {
                    strong_connect(g, w, st);
                    let lw = st.low[w];
                    let lv = st.low[v];
                    st.low.insert(v.to_string(), lv.min(lw));
                } else if st.on_stack.contains(w) {
                    let iw = st.indices[w];
                    let lv = st.low[v];
                    st.low.insert(v.to_string(), lv.min(iw));
                }
            }

            if st.low[v] == st.indices[v] {
                let mut comp = Vec::new();
                loop {
                    let w = st.stack.pop().unwrap();
                    st.on_stack.remove(&w);
                    let is_root = w == v;
                    comp.push(w);
                    if is_root {
                        break;
                    }
                }
                comp.sort();
                st.sccs.push(comp);
            }
        }

        // 按声明顺序启动，输出更可预期。
        for id in &self.order {
            if !st.indices.contains_key(id) {
                strong_connect(self, id, &mut st);
            }
        }
        st.sccs.sort();
        st.sccs
    }

    /// 在一个成环 SCC 内找一条具体环。
    /// 从 SCC 内按字典序最小的节点出发，沿 SCC 内部边 DFS，回到起点即得环。
    fn cycle_in_scc(&self, scc: &[String]) -> Option<Cycle> {
        let in_scc: HashSet<&str> = scc.iter().map(String::as_str).collect();
        let start = scc.iter().min()?.clone();

        if scc.len() == 1 {
            // 仅自环算环。
            if self.adj[&start].contains(&start) {
                return Some(Cycle {
                    nodes: vec![start.clone()],
                    edges: vec![(start.clone(), start)],
                });
            }
            return None;
        }

        // 栈中保存 (节点, 下一个待展开后继的下标)，直接作为环路径回溯。
        let mut visited: HashSet<String> = HashSet::new();
        // 栈中保存 (节点, 下一个待展开后继的下标)。
        let mut stack: Vec<(String, usize)> = vec![(start.clone(), 0)];
        visited.insert(start.clone());

        let mut found: Option<String> = None;
        while found.is_none() {
            let Some((v, step)) = stack.last_mut() else {
                break;
            };
            let successors: Vec<String> = self.adj[v]
                .iter()
                .filter(|w| in_scc.contains(w.as_str()))
                .cloned()
                .collect();
            let next = successors.get(*step).cloned();
            *step += 1;
            let Some(w) = next else {
                stack.pop();
                continue;
            };

            if w == start {
                // 栈顶 v 有一条边指回 start：找到环，退出外层循环。
                found = Some(v.clone());
                break;
            }
            if visited.insert(w.clone()) {
                stack.push((w, 0));
            }
        }

        let cycle_end = found?;
        let path: Vec<String> = stack.iter().map(|(n, _)| n.clone()).collect();
        let mut edges: Vec<(String, String)> = path
            .windows(2)
            .map(|p| (p[0].clone(), p[1].clone()))
            .collect();
        edges.push((cycle_end, start.clone()));
        Some(Cycle { nodes: path, edges })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn file(id: &str) -> Node {
        Node {
            id: id.into(),
            kind: NodeKind::File,
            rule: None,
        }
    }

    fn build(id: &str, cmd: &str) -> Node {
        Node {
            id: id.into(),
            kind: NodeKind::Build,
            rule: Some(Rule {
                command: cmd.into(),
                tool: "t".into(),
                tool_version: "1".into(),
                env: BTreeMap::new(),
            }),
        }
    }

    fn edge(a: &str, b: &str) -> Edge {
        Edge {
            from: a.into(),
            to: b.into(),
        }
    }

    fn compile(nodes: Vec<Node>, edges: Vec<Edge>) -> CompiledGraph {
        Graph { nodes, edges }.compile().expect("graph compiles")
    }

    #[test]
    fn topo_order_of_diamond_is_deterministic() {
        // a -> b,c -> d 的菱形（b/c 为 build）
        let g = compile(
            vec![file("a"), build("b", "x"), build("c", "x"), file("d")],
            vec![
                edge("a", "b"),
                edge("a", "c"),
                edge("b", "d"),
                edge("c", "d"),
            ],
        );
        let (order, remaining) = g.topo_sort();
        assert!(remaining.is_empty());
        assert_eq!(order, vec!["a", "b", "c", "d"]);
        assert!(g.find_cycles().is_empty());
    }

    #[test]
    fn duplicate_edges_are_merged() {
        let g = compile(
            vec![file("a"), build("b", "x")],
            vec![edge("a", "b"), edge("a", "b")],
        );
        assert_eq!(g.indeg["b"], 1);
        assert_eq!(g.adj["a"].len(), 1);
    }

    #[test]
    fn self_loop_is_located() {
        let g = compile(vec![build("a", "x")], vec![edge("a", "a")]);
        let cycles = g.find_cycles();
        assert_eq!(cycles.len(), 1);
        assert_eq!(cycles[0].nodes, vec!["a"]);
        assert_eq!(cycles[0].edges, vec![("a".to_string(), "a".to_string())]);
    }

    #[test]
    fn three_node_cycle_is_located_with_edges() {
        let g = compile(
            vec![build("a", "x"), build("b", "x"), build("c", "x")],
            vec![edge("a", "b"), edge("b", "c"), edge("c", "a")],
        );
        let cycles = g.find_cycles();
        assert_eq!(cycles.len(), 1);
        let cy = &cycles[0];
        // 环上的边逐节相接，最后回到起点。
        for w in cy.edges.windows(2) {
            assert_eq!(w[0].1, w[1].0);
        }
        assert_eq!(cy.edges.last().unwrap().1, cy.edges.first().unwrap().0);
        assert_eq!(cy.nodes.len(), 3);
        let (_, remaining) = g.topo_sort();
        assert_eq!(remaining.len(), 3, "all three nodes blocked by cycle");
    }

    #[test]
    fn two_disjoint_cycles_both_reported() {
        let g = compile(
            vec![
                build("a", "x"),
                build("b", "x"),
                build("c", "x"),
                build("d", "x"),
            ],
            vec![
                edge("a", "b"),
                edge("b", "a"),
                edge("c", "d"),
                edge("d", "c"),
            ],
        );
        let mut cycles = g.find_cycles();
        cycles.sort_by(|x, y| x.nodes[0].cmp(&y.nodes[0]));
        assert_eq!(cycles.len(), 2);
        assert_eq!(cycles[0].nodes, vec!["a", "b"]);
        assert_eq!(cycles[1].nodes, vec!["c", "d"]);
    }

    #[test]
    fn static_validation_collects_issues() {
        let err = Graph {
            nodes: vec![
                file("a"),
                file("a"),
                Node {
                    id: "f".into(),
                    kind: NodeKind::File,
                    rule: Some(Rule {
                        command: "x".into(),
                        tool: String::new(),
                        tool_version: String::new(),
                        env: BTreeMap::new(),
                    }),
                },
                build("g", "x"),
            ],
            edges: vec![edge("a", "ghost"), edge("g", "g")],
        }
        .compile()
        .unwrap_err();
        let codes: HashSet<&str> = err.iter().map(|i| i.code.as_str()).collect();
        assert!(codes.contains("duplicate_node"));
        assert!(codes.contains("rule_on_file"));
        assert!(codes.contains("unknown_edge_endpoint"));
        // 自环端点存在但静态校验先通过；环由 find_cycles 负责——
        // 这里 g 节点合法，自环端点也存在，因此不报 unknown。
    }

    #[test]
    fn build_node_without_rule_is_rejected() {
        let err = Graph {
            nodes: vec![Node {
                id: "b".into(),
                kind: NodeKind::Build,
                rule: None,
            }],
            edges: vec![],
        }
        .compile()
        .unwrap_err();
        assert_eq!(err[0].code, "missing_rule");
    }
}
