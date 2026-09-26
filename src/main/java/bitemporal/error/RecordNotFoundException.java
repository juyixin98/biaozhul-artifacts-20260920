package bitemporal.error;

/** 修订操作在指定有效时间区间内找不到可修订的当前版本。 */
public class RecordNotFoundException extends RuntimeException {
    public RecordNotFoundException(String message) {
        super(message);
    }
}
