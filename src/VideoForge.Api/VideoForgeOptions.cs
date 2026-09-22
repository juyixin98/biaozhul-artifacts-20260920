namespace VideoForge.Api;

public sealed class VideoForgeOptions
{
    public const string SectionName = "VideoForge";

    /// <summary>Root directory for asset storage and rendered outputs.</summary>
    public string StorageRoot { get; set; } = Path.Combine(AppContext.BaseDirectory, "var", "storage");

    /// <summary>Maximum number of jobs rendered in parallel.</summary>
    public int MaxParallelJobs { get; set; } = 3;

    /// <summary>Maximum attempts per job before it is marked failed.</summary>
    public int MaxAttempts { get; set; } = 2;

    /// <summary>Maximum accepted asset size in bytes (default 50 MB).</summary>
    public long MaxAssetBytes { get; set; } = 50L * 1024 * 1024;

    /// <summary>Allowed deviation between rendered and target duration, in seconds.</summary>
    public double DurationToleranceSeconds { get; set; } = 0.75;

    /// <summary>Seconds a single ffmpeg run may take before it is killed.</summary>
    public int RenderTimeoutSeconds { get; set; } = 300;

    /// <summary>Disables the background worker (used by tests that drive the processor manually).</summary>
    public bool DisableWorker { get; set; }

    public string AssetsDir => Path.Combine(StorageRoot, "assets");
    public string OutputsDir => Path.Combine(StorageRoot, "outputs");
    public string TmpDir => Path.Combine(StorageRoot, "outputs", "tmp");
}
