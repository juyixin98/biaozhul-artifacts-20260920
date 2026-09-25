package booleansearch.search;

import booleansearch.query.QueryNode;

/**
 * 仅供测试访问包私有的 {@link AstRenderer}。
 */
public final class AstRendererPackageTestHook {

    private AstRendererPackageTestHook() {
    }

    public static String render(QueryNode node) {
        return AstRenderer.render(node);
    }
}
