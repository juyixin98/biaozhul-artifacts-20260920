package sessions.model;

/**
 * 结果变更类型。
 *
 * <ul>
 *   <li>{@link #RETRACT}：撤回此前发布过的 (key, window, aggregate)；</li>
 *   <li>{@link #ADD}：新增一条 (key, window, aggregate) 结果。</li>
 * </ul>
 *
 * 结果从不做"原地修改"：迟到事件把已封窗并入新窗口时，
 * 先发出一条 RETRACT（旧窗口），再发出 ADD（合并后的窗口）。
 */
public enum ResultKind {
    RETRACT,
    ADD
}
