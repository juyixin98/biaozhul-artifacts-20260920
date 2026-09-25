package drvb.version;

/** 版本 / 绑定操作被拒绝时抛出，携带稳定错误码供 HTTP 层映射。 */
public class VersionException extends RuntimeException {

    private final String code;

    public VersionException(String code, String message) {
        super(message);
        this.code = code;
    }

    public String code() {
        return code;
    }
}
