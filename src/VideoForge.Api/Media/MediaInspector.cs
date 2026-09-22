using System.Diagnostics;
using System.Text.Json;
using VideoForge.Api.Configuration;

namespace VideoForge.Api.Media;

public sealed record MediaProbe(
    string Kind,          // "image" | "video"
    string CodecName,
    string PixelFormat,
    int Width,
    int Height,
    long? DurationMs,
    string Extension);

public sealed class InvalidMediaException(string message) : Exception(message);

/// <summary>
/// Validates uploads by their actual content: first the "magic bytes"
/// signature, then ffprobe. A renamed .exe or a truncated file is rejected
/// even when its extension/MIME type claim is correct.
/// </summary>
public sealed class MediaInspector(VideoForgeOptions options, ILogger<MediaInspector> logger)
{
    // (offset, signature bytes, canonical extension)
    private static readonly (int Off, byte[] Sig, string Ext)[] ImageSignatures =
    [
        (0, [0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A], ".png"), // PNG
        (0, [0xFF, 0xD8, 0xFF], ".jpg"),                               // JPEG
    ];

    private static readonly byte[] Riff = [0x52, 0x49, 0x46, 0x46];
    private static readonly byte[] Webp = [0x57, 0x45, 0x42, 0x50];

    private static readonly HashSet<string> VideoCodecs =
        new(StringComparer.OrdinalIgnoreCase) { "h264", "hevc", "mpeg4", "vp8", "vp9", "av1" };
    private static readonly HashSet<string> ImageCodecs =
        new(StringComparer.OrdinalIgnoreCase) { "mjpeg", "png", "webp" };

    public async Task<MediaProbe> InspectAsync(string path, CancellationToken ct)
    {
        var head = await ReadHeadAsync(path, ct);
        var detected = DetectBySignature(head)
            ?? throw new InvalidMediaException(
                "Unsupported file type: file content does not match an allowed image (PNG/JPEG/WebP) or video (MP4/MOV/WebM).");

        var probe = await RunFfprobeAsync(path, ct);
        if (probe.Width <= 0 || probe.Height <= 0)
            throw new InvalidMediaException("Media has no usable dimensions according to ffprobe.");

        var kind = ReconcileKind(detected.Kind, probe.CodecName, detected.Extension, probe.DurationMs);
        return probe with { Kind = kind, Extension = detected.Extension };
    }

    private static string ReconcileKind(string signatureKind, string codec, string extension, long? durationMs)
    {
        if (signatureKind == "image")
        {
            if (ImageCodecs.Contains(codec))
            {
                // An animated WebP reports VP8/VP9; accept only still webp images.
                if (string.Equals(extension, ".webp", StringComparison.OrdinalIgnoreCase) &&
                    VideoCodecs.Contains(codec))
                    throw new InvalidMediaException("Animated WebP is not supported; provide a still image or an MP4 video.");
                return "image";
            }
            throw new InvalidMediaException(
                $"Content mismatch: image signature but video/unsupported codec '{codec}'.");
        }

        // video container
        if (VideoCodecs.Contains(codec) ||
            codec.Equals("mjpeg", StringComparison.OrdinalIgnoreCase)) // motion-JPEG video is fine
            return "video";

        throw new InvalidMediaException($"Video container contains an unsupported codec '{codec}'.");
    }

    internal sealed record SignatureDetection(string Kind, string Extension);

    internal static SignatureDetection? DetectBySignature(byte[] h)
    {
        if (h.Length >= 8)
        {
            foreach (var (off, sig, ext) in ImageSignatures)
                if (MatchAt(h, off, sig))
                    return new SignatureDetection("image", ext);
        }
        if (h.Length >= 12 && MatchAt(h, 0, Riff) && MatchAt(h, 8, Webp))
            return new SignatureDetection("image", ".webp");
        if (h.Length >= 12 && h[4] == 0x66 && h[5] == 0x74 && h[6] == 0x79 && h[7] == 0x70)
        {
            // ftyp: ISO BMFF. Brand 'qt' at offset 8 indicates a QuickTime MOV.
            var isMov = h[8] == 0x71 && h[9] == 0x74;
            return new SignatureDetection("video", isMov ? ".mov" : ".mp4");
        }
        if (h.Length >= 4 && h[0] == 0x1A && h[1] == 0x45 && h[2] == 0xDF && h[3] == 0xA3)
            return new SignatureDetection("video", ".webm"); // Matroska / WebM EBML header
        return null;
    }

    private static bool MatchAt(byte[] data, int offset, byte[] sig)
    {
        if (offset + sig.Length > data.Length) return false;
        for (var i = 0; i < sig.Length; i++)
            if (data[offset + i] != sig[i]) return false;
        return true;
    }

    private static async Task<byte[]> ReadHeadAsync(string path, CancellationToken ct)
    {
        await using var fs = File.OpenRead(path);
        var buffer = new byte[Math.Min(64, (int)fs.Length)];
        var read = 0;
        while (read < buffer.Length)
        {
            var n = await fs.ReadAsync(buffer.AsMemory(read), ct);
            if (n == 0) break;
            read += n;
        }
        return buffer[..read];
    }

    private async Task<MediaProbe> RunFfprobeAsync(string path, CancellationToken ct)
    {
        var psi = new ProcessStartInfo
        {
            FileName = options.FFprobePath,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        psi.ArgumentList.Add("-v");
        psi.ArgumentList.Add("error");
        psi.ArgumentList.Add("-print_format");
        psi.ArgumentList.Add("json");
        psi.ArgumentList.Add("-show_format");
        psi.ArgumentList.Add("-show_streams");
        psi.ArgumentList.Add(path);

        using var proc = Process.Start(psi)
            ?? throw new InvalidOperationException($"Cannot start {options.FFprobePath}");
        var stdout = await proc.StandardOutput.ReadToEndAsync(ct);
        var stderr = await proc.StandardError.ReadToEndAsync(ct);
        await proc.WaitForExitAsync(ct);
        if (proc.ExitCode != 0)
            throw new InvalidMediaException($"ffprobe rejected the file: {stderr.Trim()}");

        try
        {
            using var doc = JsonDocument.Parse(stdout);
            var root = doc.RootElement;
            long? durationMs = null;
            if (root.TryGetProperty("format", out var fmt) &&
                fmt.TryGetProperty("duration", out var durEl) &&
                double.TryParse(durEl.GetString(), System.Globalization.NumberStyles.Float,
                    System.Globalization.CultureInfo.InvariantCulture, out var dur) && dur > 0)
            {
                durationMs = (long)Math.Round(dur * 1000);
            }

            foreach (var s in root.GetProperty("streams").EnumerateArray())
            {
                var codec = s.TryGetProperty("codec_type", out var ctEl) ? ctEl.GetString() : null;
                if (codec != "video") continue;
                var codecName = s.GetProperty("codec_name").GetString() ?? "";
                var pix = s.TryGetProperty("pix_fmt", out var pf) ? pf.GetString() : "";
                var w = s.GetProperty("width").GetInt32();
                var h = s.GetProperty("height").GetInt32();
                return new MediaProbe("video", codecName, pix ?? "", w, h, durationMs, "");
            }
            throw new InvalidMediaException("No video/image stream found in file.");
        }
        catch (InvalidMediaException)
        {
            throw;
        }
        catch (Exception ex)
        {
            logger.LogWarning(ex, "Failed parsing ffprobe output: {Out}", stdout);
            throw new InvalidMediaException("Could not parse ffprobe output for the uploaded file.");
        }
    }
}
