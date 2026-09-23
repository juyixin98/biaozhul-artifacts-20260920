package colscan.store;

import java.io.FilterInputStream;
import java.io.IOException;
import java.io.InputStream;

/**
 * 统计实际从底层流读取的字节数。
 * read(byte[]) / read(b,off,len) 按实际返回字节数累加，保证“读了多少报多少”。
 */
public final class CountingInputStream extends FilterInputStream {

    private long bytes;

    public CountingInputStream(InputStream in) {
        super(in);
    }

    public long bytesRead() {
        return bytes;
    }

    @Override
    public int read() throws IOException {
        int b = in.read();
        if (b >= 0) bytes++;
        return b;
    }

    @Override
    public int read(byte[] b, int off, int len) throws IOException {
        int n = in.read(b, off, len);
        if (n > 0) bytes += n;
        return n;
    }
}
