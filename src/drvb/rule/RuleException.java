package drvb.rule;

/** 规则定义 / 谓词规约非法（发布时即拒绝，不产生版本）。 */
public class RuleException extends RuntimeException {
    public RuleException(String message) {
        super(message);
    }
}
