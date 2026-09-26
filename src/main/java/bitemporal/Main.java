package bitemporal;

import bitemporal.api.ApiResponse;
import bitemporal.api.BatchStep;
import bitemporal.api.BitemporalService;
import bitemporal.error.OverlapRejectedException;
import bitemporal.error.RecordNotFoundException;
import bitemporal.error.ValidationException;
import bitemporal.json.JsonMapper;
import bitemporal.model.QueryRequest;
import bitemporal.model.TransactionRequest;
import bitemporal.store.PersistentBitemporalStore;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.io.InputStream;
import java.io.PrintStream;
import java.io.UncheckedIOException;
import java.nio.file.Path;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * 纯后端命令行入口：从标准输入读取一个 JSON 请求，向标准输出写一个 JSON 响应。
 *
 * <pre>
 *   java -jar bitemporal-records-1.0.0.jar seed
 *   echo '{...}' | java -jar bitemporal-records-1.0.0.jar commit
 *   echo '{...}' | java -jar bitemporal-records-1.0.0.jar query
 *   echo '{...}' | java -jar bitemporal-records-1.0.0.jar history
 *   java -jar bitemporal-records-1.0.0.jar reset
 *   java -jar bitemporal-records-1.0.0.jar info
 *   echo '{...}' | java -jar bitemporal-records-1.0.0.jar batch
 * </pre>
 *
 * 退出码：0 成功；2 业务错误（参数校验/重叠拒绝/记录不存在）；1 其他内部错误。
 */
public final class Main {

    static final int EXIT_OK = 0;
    static final int EXIT_INTERNAL = 1;
    static final int EXIT_BUSINESS = 2;

    private Main() {
    }

    public static void main(String[] args) {
        Path dbPath = Path.of(System.getenv().getOrDefault("BITEMPORAL_DB", "bitemporal-db.json"));
        int exit = run(args, System.in, System.out, System.err, dbPath);
        System.exit(exit);
    }

    /**
     * 可测试的命令执行体：不调用 {@code System.exit}，所有 IO 走参数注入。
     *
     * @return 进程退出码
     */
    static int run(String[] args,
                   InputStream stdin,
                   PrintStream stdout,
                   PrintStream stderr,
                   Path dbPath) {
        ObjectMapper mapper = JsonMapper.get();
        PersistentBitemporalStore store;
        try {
            store = new PersistentBitemporalStore(dbPath);
        } catch (UncheckedIOException e) {
            stderr.println(e.getMessage());
            return EXIT_INTERNAL;
        }
        BitemporalService service = new BitemporalService(store);

        if (args.length == 0) {
            stderr.println("missing command; expected one of: seed | commit | query | history "
                    + "| reset | info | batch");
            return EXIT_INTERNAL;
        }
        String command = args[0].trim().toLowerCase(Locale.ROOT);

        try {
            Object data = dispatch(command, service, mapper, stdin);
            return write(stdout, mapper, ApiResponse.ok(data));
        } catch (JsonProcessingException | IllegalArgumentException e) {
            return write(stdout, mapper, ApiResponse.fail("VALIDATION_ERROR",
                    "invalid JSON request: " + e.getMessage(), null), EXIT_BUSINESS);
        } catch (ValidationException e) {
            return write(stdout, mapper,
                    ApiResponse.fail("VALIDATION_ERROR", e.getMessage(), null), EXIT_BUSINESS);
        } catch (OverlapRejectedException e) {
            return write(stdout, mapper,
                    ApiResponse.fail("OVERLAP_REJECTED", e.getMessage(), e.recordId()),
                    EXIT_BUSINESS);
        } catch (RecordNotFoundException e) {
            return write(stdout, mapper,
                    ApiResponse.fail("RECORD_NOT_FOUND", e.getMessage(), null), EXIT_BUSINESS);
        } catch (Exception e) {
            return write(stdout, mapper,
                    ApiResponse.fail("INTERNAL_ERROR", String.valueOf(e.getMessage()), null),
                    EXIT_INTERNAL);
        }
    }

    private static Object dispatch(String command,
                                   BitemporalService service,
                                   ObjectMapper mapper,
                                   InputStream stdin) throws IOException {
        return switch (command) {
            case "seed" -> service.seed();
            case "reset" -> {
                service.reset();
                yield Map.of("reset", true);
            }
            case "info" -> service.info();
            case "commit" -> service.commit(readBody(mapper, stdin, TransactionRequest.class));
            case "query" -> service.query(readBody(mapper, stdin, QueryRequest.class));
            case "history" -> service.history(readBody(mapper, stdin, HistoryBody.class).recordId());
            case "batch" -> service.batch(readBody(mapper, stdin, BatchBody.class).steps());
            default -> throw new IllegalArgumentException("unknown command '" + command
                    + "'; expected one of: seed | commit | query | history | reset | info | batch");
        };
    }

    private static <T> T readBody(ObjectMapper mapper, InputStream stdin, Class<T> type)
            throws IOException {
        T body = mapper.readValue(stdin, type);
        if (body == null) {
            throw new IllegalArgumentException("request body must not be empty");
        }
        return body;
    }

    private static int write(PrintStream stdout, ObjectMapper mapper, ApiResponse response) {
        return write(stdout, mapper, response, EXIT_OK);
    }

    private static int write(PrintStream stdout, ObjectMapper mapper, ApiResponse response,
                             int exitCode) {
        try {
            stdout.println(mapper.writerWithDefaultPrettyPrinter().writeValueAsString(response));
            return exitCode;
        } catch (JsonProcessingException e) {
            return EXIT_INTERNAL;
        }
    }

    private record HistoryBody(String recordId) {
    }

    private record BatchBody(List<BatchStep> steps) {
    }
}
