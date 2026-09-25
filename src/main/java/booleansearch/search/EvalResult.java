package booleansearch.search;

import java.util.List;
import java.util.TreeSet;

/**
 * 一次查询求值的结果：命中文档、工作量计数与计划轨迹。
 *
 * @param docIds            命中文档 id（有序）
 * @param postReads         从倒排链读取的文档 id 数（词项叶子的工作量）
 * @param membershipProbes  集合成员判断次数（AND 交集 / NOT 差集的 contains 探测）
 * @param trace             人类可读的执行计划/过程描述
 */
public record EvalResult(TreeSet<Integer> docIds,
                         long postReads,
                         long membershipProbes,
                         List<String> trace) {
}
