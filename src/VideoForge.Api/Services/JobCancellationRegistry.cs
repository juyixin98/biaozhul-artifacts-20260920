using System.Collections.Concurrent;

namespace VideoForge.Api.Services;

/// <summary>
/// Tracks in-process cancellation for jobs being rendered on this node.
/// The database status is the source of truth; this only speeds up aborting ffmpeg.
/// </summary>
public sealed class JobCancellationRegistry
{
    private readonly ConcurrentDictionary<long, CancellationTokenSource> _tokens = new();

    public CancellationToken Register(long jobId) =>
        _tokens.GetOrAdd(jobId, _ => new CancellationTokenSource()).Token;

    public void Cancel(long jobId)
    {
        if (_tokens.TryGetValue(jobId, out var cts))
            cts.Cancel();
    }

    public void Unregister(long jobId)
    {
        if (_tokens.TryRemove(jobId, out var cts))
            cts.Dispose();
    }
}
