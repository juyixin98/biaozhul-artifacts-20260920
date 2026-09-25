package booleansearch.search;

import booleansearch.index.InvertedIndex;
import booleansearch.model.Document;
import booleansearch.query.QueryNode;
import booleansearch.query.QueryParseException;
import booleansearch.query.QueryParser;

import java.util.List;
import java.util.TreeSet;

/**
 * 搜索引擎外观：解析查询，并用"朴素"与"优化"两种计划各执行一次，
 * 返回两份结果与工作量计数，方便对照（结果必须一致，工作量可以不同）。
 */
public final class SearchEngine {

    private final InvertedIndex index;

    public SearchEngine(InvertedIndex index) {
        this.index = index;
    }

    /**
     * 执行一次查询。
     *
     * @throws QueryParseException 语法错误（携带位置）
     */
    public SearchOutcome search(String query) {
        QueryNode ast = QueryParser.parse(query);
        String rendered = AstRenderer.render(ast);

        EvalResult naive = new NaiveEvaluator(index).evaluate(ast);
        EvalResult optimized = new OptimizedPlanner(index).evaluate(ast);

        boolean identical = naive.docIds().equals(optimized.docIds());
        return new SearchOutcome(
                query,
                rendered,
                documentsFor(naive.docIds()),
                naive,
                optimized,
                identical,
                optimized.membershipProbes() - naive.membershipProbes(),
                index.liveDocIds().size());
    }

    private List<DocumentView> documentsFor(TreeSet<Integer> ids) {
        return ids.stream()
                .map(index::getDocument)
                .filter(java.util.Objects::nonNull)
                .map(d -> new DocumentView(d.id(), d.title()))
                .toList();
    }

    /** 查询执行的完整结果。 */
    public record SearchOutcome(
            String query,
            String normalizedAst,
            List<DocumentView> hits,
            EvalResult naive,
            EvalResult optimized,
            boolean resultsIdentical,
            long probeDelta,
            int universeSize) {
    }

    public record DocumentView(int id, String title) {
    }
}
