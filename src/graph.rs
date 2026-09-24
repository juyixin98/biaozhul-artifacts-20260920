// Dependency graph: nodes, dependency edges, cycle detection (Tarjan SCC),
// and reverse-edge reachability used for copyleft obligation propagation.
//
// Edge direction: `from` (consumer/package) -> `to` (its dependency).
// Copyleft obligations propagate in the *reverse* direction, from a
// dependency up to every package that (transitively) consumes it.

use std::collections::{BTreeMap, HashSet};

use crate::model::{EdgeHop, Link};

#[derive(Debug, Clone)]
pub struct Edge {
    pub from: usize,
    pub to: usize,
    pub link: Link,
}

#[derive(Debug, Clone)]
pub struct Graph {
    pub ids: Vec<String>,
    pub out_edges: Vec<Vec<Edge>>,
    pub in_edges: Vec<Vec<Edge>>,
}

impl Graph {
    pub fn build(
        package_ids: &[String],
        raw_edges: &[(String, String, Link)],
    ) -> Result<Graph, String> {
        let mut index = BTreeMap::new();
        for (i, id) in package_ids.iter().enumerate() {
            if index.insert(id.clone(), i).is_some() {
                return Err(format!("duplicate package id '{}'", id));
            }
        }
        let n = package_ids.len();
        let mut out_edges = vec![Vec::new(); n];
        let mut in_edges = vec![Vec::new(); n];
        for (from, to, link) in raw_edges {
            let f = *index
                .get(from)
                .ok_or_else(|| format!("edge references unknown package '{}'", from))?;
            let t = *index
                .get(to)
                .ok_or_else(|| format!("edge references unknown package '{}'", to))?;
            let e = Edge {
                from: f,
                to: t,
                link: *link,
            };
            out_edges[f].push(e.clone());
            in_edges[t].push(e);
        }
        Ok(Graph {
            ids: package_ids.to_vec(),
            out_edges,
            in_edges,
        })
    }

    #[allow(dead_code)]
    pub fn is_root(&self, node: usize) -> bool {
        self.in_edges[node].is_empty()
    }

    /// Incoming link contexts of a package: distinct links on edges into it.
    pub fn incoming_contexts(&self, node: usize) -> Vec<Link> {
        let mut v: Vec<Link> = self.in_edges[node].iter().map(|e| e.link).collect();
        v.sort_by_key(|l| match l {
            Link::Static => 0,
            Link::Dynamic => 1,
        });
        v.dedup();
        v
    }

    /// Nodes reached by propagating an obligation from `start` across reverse
    /// edges whose link type is in `prop_links`. Includes `start` itself
    /// (a package is itself in scope of its own copyleft license).
    pub fn reverse_reachable(start: usize, prop_links: &[Link], g: &Graph) -> HashSet<usize> {
        let mut seen = HashSet::new();
        seen.insert(start);
        let mut stack = vec![start];
        while let Some(v) = stack.pop() {
            for e in &g.in_edges[v] {
                if prop_links.contains(&e.link) && seen.insert(e.from) {
                    stack.push(e.from);
                }
            }
        }
        seen
    }

    /// Shortest original-graph path from `target` (consumer) down to `source`
    /// (dependency), returning its edges in that top-down order.
    /// Returns None if source is not a transitive dependency of target.
    pub fn obligation_path(
        source: usize,
        target: usize,
        prop_links: &[Link],
        g: &Graph,
    ) -> Option<Vec<EdgeHop>> {
        if source == target {
            return Some(Vec::new());
        }
        // BFS on the reverse graph. predecessor[u] = (v, link) where u -> v.
        let mut pred: Vec<Option<(usize, Link)>> = vec![None; g.ids.len()];
        let mut seen = HashSet::new();
        seen.insert(source);
        let mut queue = std::collections::VecDeque::new();
        queue.push_back(source);
        while let Some(v) = queue.pop_front() {
            for e in &g.in_edges[v] {
                if !prop_links.contains(&e.link) || !seen.insert(e.from) {
                    continue;
                }
                pred[e.from] = Some((v, e.link));
                if e.from == target {
                    queue.clear();
                    break;
                }
                queue.push_back(e.from);
            }
        }
        // Walk predecessor chain target -> ... -> source. Each predecessor
        // pair `(down, link)` already records the original edge cur -> down,
        // so the collected hops are in top-down order.
        let mut hops: Vec<(usize, usize, Link)> = Vec::new();
        let mut cur = target;
        while cur != source {
            let (down, link) = pred[cur]?;
            hops.push((cur, down, link));
            cur = down;
        }
        Some(
            hops
                .into_iter()
                .map(|(from, to, link)| EdgeHop {
                    from: g.ids[from].clone(),
                    to: g.ids[to].clone(),
                    link: match link {
                        Link::Static => "static".to_string(),
                        Link::Dynamic => "dynamic".to_string(),
                    },
                })
                .collect(),
        )
    }

    /// Tarjan strongly connected components; returns SCCs with size > 1 or
    /// containing a self-loop (i.e., the SCCs that contain a cycle).
    pub fn cyclic_sccs(&self) -> Vec<Vec<usize>> {
        let n = self.ids.len();
        let mut idx = 0usize;
        let mut index = vec![u32::MAX; n];
        let mut lowlink = vec![0u32; n];
        let mut on_stack = vec![false; n];
        let mut stack: Vec<usize> = Vec::new();
        let mut sccs: Vec<Vec<usize>> = Vec::new();

        // Iterative Tarjan to avoid recursion-depth concerns.
        struct Frame {
            v: usize,
            next: usize,
        }
        for root in 0..n {
            if index[root] != u32::MAX {
                continue;
            }
            index[root] = idx as u32;
            lowlink[root] = idx as u32;
            idx += 1;
            stack.push(root);
            on_stack[root] = true;
            let mut work = vec![Frame { v: root, next: 0 }];
            while let Some(frame) = work.last_mut() {
                let v = frame.v;
                if frame.next < self.out_edges[v].len() {
                    let e = self.out_edges[v][frame.next].clone();
                    frame.next += 1;
                    if index[e.to] == u32::MAX {
                        index[e.to] = idx as u32;
                        lowlink[e.to] = idx as u32;
                        idx += 1;
                        stack.push(e.to);
                        on_stack[e.to] = true;
                        work.push(Frame { v: e.to, next: 0 });
                    } else if on_stack[e.to] {
                        lowlink[v] = lowlink[v].min(index[e.to]);
                    }
                } else {
                    if lowlink[v] == index[v] {
                        let mut comp = Vec::new();
                        loop {
                            let w = stack.pop().unwrap();
                            on_stack[w] = false;
                            comp.push(w);
                            if w == v {
                                break;
                            }
                        }
                        let self_loop = comp.iter().any(|u| {
                            self.out_edges[*u].iter().any(|e| e.to == *u)
                        });
                        if comp.len() > 1 || self_loop {
                            comp.sort_unstable();
                            sccs.push(comp);
                        }
                    }
                    work.pop();
                    if let Some(parent) = work.last() {
                        let p = parent.v;
                        lowlink[p] = lowlink[p].min(lowlink[v]);
                    }
                }
            }
        }
        sccs
    }

    /// Extract one concrete cycle inside a cyclic SCC, starting from its
    /// first node. Nodes are returned in cycle order (start ... back to
    /// start), edges in the same order.
    pub fn cycle_in_scc(&self, scc: &[usize]) -> (Vec<String>, Vec<EdgeHop>) {
        let in_scc: HashSet<usize> = scc.iter().copied().collect();
        let start = scc[0];
        // Self-loop fast path.
        if let Some(e) = self.out_edges[start].iter().find(|e| e.to == start) {
            return (vec![self.ids[start].clone()], vec![self.to_hop(e)]);
        }
        // DFS restricted to the SCC, never re-entering `start`; the first
        // edge from a reached node back to `start` closes a simple cycle.
        // parent[v] records the tree edge parent[v] -> v.
        let mut parent: Vec<Option<(usize, Link)>> = vec![None; self.ids.len()];
        let mut closing: Option<(usize, Link)> = None;

        fn dfs(
            v: usize,
            start: usize,
            graph: &Graph,
            in_scc: &HashSet<usize>,
            parent: &mut [Option<(usize, Link)>],
            closing: &mut Option<(usize, Link)>,
        ) {
            if closing.is_some() {
                return;
            }
            for e in &graph.out_edges[v] {
                if !in_scc.contains(&e.to) {
                    continue;
                }
                if e.to == start {
                    *closing = Some((v, e.link));
                    return;
                }
                if parent[e.to].is_none() {
                    parent[e.to] = Some((v, e.link));
                    dfs(e.to, start, graph, in_scc, parent, closing);
                    if closing.is_some() {
                        return;
                    }
                }
            }
        }
        parent[start] = Some((start, Link::Static));
        dfs(start, start, self, &in_scc, &mut parent, &mut closing);

        let (closer, close_link) = closing.expect("cycle exists inside cyclic SCC");
        // Walk parents from `closer` up to (and including) `start`, then
        // append the closing edge closer -> start.
        let mut chain_nodes = vec![closer];
        let mut chain_edges: Vec<(usize, usize, Link)> = Vec::new();
        let mut cur = closer;
        while cur != start {
            let (up, link) = parent[cur].expect("tree parent");
            chain_edges.push((up, cur, link));
            cur = up;
            chain_nodes.push(cur);
        }
        chain_nodes.reverse();
        chain_edges.reverse();
        chain_edges.push((closer, start, close_link));

        let nodes: Vec<String> = chain_nodes
            .iter()
            .map(|u| self.ids[*u].clone())
            .collect();
        let edges: Vec<EdgeHop> = chain_edges
            .into_iter()
            .map(|(from, to, link)| EdgeHop {
                from: self.ids[from].clone(),
                to: self.ids[to].clone(),
                link: match link {
                    Link::Static => "static".to_string(),
                    Link::Dynamic => "dynamic".to_string(),
                },
            })
            .collect();
        (nodes, edges)
    }

    fn to_hop(&self, e: &Edge) -> EdgeHop {
        EdgeHop {
            from: self.ids[e.from].clone(),
            to: self.ids[e.to].clone(),
            link: match e.link {
                Link::Static => "static".to_string(),
                Link::Dynamic => "dynamic".to_string(),
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn g(ids: &[&str], edges: &[(&str, &str, Link)]) -> Graph {
        let ids: Vec<String> = ids.iter().map(|s| s.to_string()).collect();
        let raw: Vec<(String, String, Link)> = edges
            .iter()
            .map(|(a, b, l)| (a.to_string(), b.to_string(), *l))
            .collect();
        Graph::build(&ids, &raw).unwrap()
    }

    #[test]
    fn detects_two_node_cycle_and_self_loop() {
        let graph = g(
            &["a", "b", "c", "d"],
            &[
                ("a", "b", Link::Static),
                ("b", "a", Link::Dynamic),
                ("c", "c", Link::Static),
                ("a", "d", Link::Static),
            ],
        );
        let sccs = graph.cyclic_sccs();
        assert_eq!(sccs.len(), 2);
        let two = sccs.iter().find(|s| s.len() == 2).unwrap();
        let (nodes, edges) = graph.cycle_in_scc(two);
        assert_eq!(nodes.len(), 2);
        assert_eq!(edges.len(), 2);
        // edges close the loop: last hop returns to first node
        assert_eq!(edges.last().unwrap().to, nodes[0]);
    }

    #[test]
    fn reverse_reach_respects_link_types() {
        let graph = g(
            &["app", "mid", "lib"],
            &[
                ("app", "mid", Link::Dynamic),
                ("mid", "lib", Link::Static),
            ],
        );
        // strong propagates on both links
        let strong = Graph::reverse_reachable(2, &[Link::Static, Link::Dynamic], &graph);
        assert_eq!(strong.len(), 3);
        // weak (static-only) reaches mid but not app
        let weak = Graph::reverse_reachable(2, &[Link::Static], &graph);
        assert!(weak.contains(&1));
        assert!(!weak.contains(&0));
    }

    #[test]
    fn obligation_path_top_down_order() {
        let graph = g(
            &["app", "mid", "lib"],
            &[
                ("app", "mid", Link::Static),
                ("mid", "lib", Link::Static),
            ],
        );
        let hops =
            Graph::obligation_path(2, 0, &[Link::Static], &graph).expect("path exists");
        assert_eq!(hops.len(), 2);
        assert_eq!(hops[0].from, "app");
        assert_eq!(hops[0].to, "mid");
        assert_eq!(hops[1].from, "mid");
        assert_eq!(hops[1].to, "lib");
        assert_eq!(Graph::obligation_path(0, 2, &[Link::Static], &graph), None);
    }

    #[test]
    fn incoming_contexts() {
        let graph = g(
            &["a", "b", "c"],
            &[
                ("a", "c", Link::Static),
                ("b", "c", Link::Dynamic),
            ],
        );
        assert_eq!(graph.incoming_contexts(2), vec![Link::Static, Link::Dynamic]);
        assert!(graph.is_root(0));
    }

    #[test]
    fn unknown_edge_endpoint_errors() {
        let ids = vec!["a".to_string()];
        let raw = vec![("a".to_string(), "ghost".to_string(), Link::Static)];
        assert!(Graph::build(&ids, &raw).is_err());
    }
}
