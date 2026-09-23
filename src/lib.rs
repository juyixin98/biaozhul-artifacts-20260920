//! 磁盘 B+ 树整数键索引服务。
//!
//! 模块组织：
//! - [`pager`]：定长页磁盘/内存存储与页分配回收。
//! - [`node`]：叶/内部节点的页格式与二进制编解码。
//! - [`bptree`]：B+ 树插入分裂、删除借位/合并、叶链维护与全树校验。
//! - [`http`]：基于 Axum 的 HTTP 接口。

pub mod bptree;
pub mod http;
pub mod node;
pub mod pager;

pub use bptree::{BPTree, TreeStats};
pub use node::{InternalNode, LeafNode, Node};
pub use pager::{FilePager, MemoryPager, PageId, Pager};
