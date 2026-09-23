package windowengine.plan;

import windowengine.EngineException;
import windowengine.ErrorCode;

/** 帧模式：本引擎只实现 SQL 的 ROWS（物理行偏移）。 */
public enum FrameMode {
    ROWS;

    public static FrameMode parse(String raw) {
        if (!"ROWS".equalsIgnoreCase(raw)) {
            throw new EngineException(ErrorCode.UNSUPPORTED,
                    "仅支持 ROWS 帧模式，不支持: " + raw + "（RANGE/GROUPS 未实现）");
        }
        return ROWS;
    }
}
