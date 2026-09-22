using System.Security.Cryptography;
using VideoForge.Api.Configuration;

namespace VideoForge.Api.Storage;

/// <summary>
/// All filesystem paths flow through this class. Filenames are replaced with
/// generated names (never user-controlled) and published-file lookups are
/// confined to PublishedDir to prevent path traversal.
/// </summary>
public sealed class FileStorage(VideoForgeOptions options)
{
    public void EnsureDirectories()
    {
        Directory.CreateDirectory(options.AssetsDir);
        Directory.CreateDirectory(options.TempDir);
        Directory.CreateDirectory(options.PublishedDir);
    }

    public string AssetPath(Guid id, string extension) =>
        Path.Combine(options.AssetsDir, $"{id:N}{NormalizeExtension(extension)}");

    /// <summary>Extensionless staging path used until content-based inspection succeeds.</summary>
    public string IncomingUploadPath(Guid id) =>
        Path.Combine(options.TempDir, $"incoming-{id:N}.part");

    public string TempOutputPath(Guid jobId, int attemptNo) =>
        Path.Combine(options.TempDir, $"job-{jobId:N}-attempt-{attemptNo}.mp4");

    public string TempClipPath(Guid jobId, int attemptNo, int position) =>
        Path.Combine(options.TempDir, $"job-{jobId:N}-a{attemptNo}-s{position:D3}.mp4");

    public string ConcatListPath(Guid jobId, int attemptNo) =>
        Path.Combine(options.TempDir, $"job-{jobId:N}-a{attemptNo}.txt");

    public string PublishedPath(Guid jobId) =>
        Path.Combine(options.PublishedDir, $"videoforge-{jobId:N}.mp4");

    /// <summary>
    /// Attempt-specific published path. A stale worker whose claim was reaped
    /// can finish rendering after a retry already published: distinct paths
    /// mean it can never clobber the newer result.
    /// </summary>
    public string AttemptPublishedPath(Guid jobId, int attemptNo) =>
        Path.Combine(options.PublishedDir, $"videoforge-{jobId:N}-a{attemptNo}.mp4");

    /// <summary>
    /// True only when candidate resolves to a file inside PublishedDir.
    /// Guard against symlink/path-escape tricks even though the stored path
    /// is always generated server-side.
    /// </summary>
    public bool IsWithinPublished(string candidate)
    {
        var publishedRoot = Path.GetFullPath(options.PublishedDir)
            .TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        var full = Path.GetFullPath(candidate);
        return full.StartsWith(publishedRoot, StringComparison.Ordinal);
    }

    /// <summary>
    /// Streams an upload to disk, enforcing the size cap while copying (no
    /// unbounded buffering), then returns (path, bytes, sha256).
    /// </summary>
    public async Task<(string Path, long Length, string Sha256)> SaveUploadAsync(
        Stream source, string target, CancellationToken ct)
    {
        EnsureDirectories();
        try
        {
            await using var fs = new FileStream(target, FileMode.CreateNew, FileAccess.Write, FileShare.None, 81920,
                FileOptions.Asynchronous | FileOptions.SequentialScan);
            using var sha = SHA256.Create();
            using var counting = new CryptoStream(fs, sha, CryptoStreamMode.Write);
            var buffer = new byte[81920];
            long total = 0;
            while (true)
            {
                var n = await source.ReadAsync(buffer, ct);
                if (n == 0) break;
                total += n;
                if (total > options.MaxMaterialBytes)
                    throw new AssetTooLargeException(options.MaxMaterialBytes);
                await counting.WriteAsync(buffer.AsMemory(0, n), ct);
            }
            await counting.FlushFinalBlockAsync(ct);
            return (target, total, Convert.ToHexString(sha.Hash!).ToLowerInvariant());
        }
        catch
        {
            TryDelete(target);
            throw;
        }
    }

    /// <summary>True if the given job's published file exists at its canonical path.</summary>
    public bool TryGetPublishedPath(Guid jobId, out string path)
    {
        path = PublishedPath(jobId);
        return File.Exists(path);
    }

    public void TryDelete(string path)
    {
        try { if (File.Exists(path)) File.Delete(path); }
        catch { /* best effort */ }
    }

    /// <summary>Deletes temp files left by attempts that died mid-render.</summary>
    public void DeleteJobTempArtifacts(Guid jobId)
    {
        foreach (var file in Directory.EnumerateFiles(options.TempDir, $"job-{jobId:N}-*"))
            TryDelete(file);
    }

    /// <summary>Startup sweep: temp dir only ever holds in-flight attempt files.</summary>
    public void ClearStaleTempFiles()
    {
        EnsureDirectories();
        foreach (var file in Directory.EnumerateFiles(options.TempDir))
        {
            try
            {
                var age = DateTime.UtcNow - File.GetLastWriteTimeUtc(file);
                if (age > TimeSpan.FromMinutes(10)) File.Delete(file);
            }
            catch { /* best effort */ }
        }
    }

    // Extension is whitelisted: callers only pass extensions derived from
    // signature-based type detection, never from the upload filename.
    private static string NormalizeExtension(string extension)
    {
        var ext = extension.Trim().ToLowerInvariant();
        return ext switch
        {
            "png" or ".png" => ".png",
            "jpg" or "jpeg" or ".jpg" or ".jpeg" => ".jpg",
            "webp" or ".webp" => ".webp",
            "mp4" or ".mp4" => ".mp4",
            "mov" => ".mov",
            "webm" => ".webm",
            "mkv" => ".mkv",
            _ => throw new ArgumentException($"Unsupported extension '{extension}'")
        };
    }
}

public sealed class AssetTooLargeException(long limitBytes)
    : Exception($"Material exceeds the maximum allowed size of {limitBytes} bytes ({limitBytes / 1024 / 1024} MB).")
{
    public long LimitBytes { get; } = limitBytes;
}
