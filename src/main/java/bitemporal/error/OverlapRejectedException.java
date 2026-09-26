package bitemporal.error;

/** 业务规则冲突：业务有效时间与同一记录的当前版本重叠（半开相邻不算重叠）。 */
public class OverlapRejectedException extends RuntimeException {

    private final String recordId;

    public OverlapRejectedException(String recordId, String message) {
        super(message);
        this.recordId = recordId;
    }

    public String recordId() {
        return recordId;
    }
}
