FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src

COPY ["src/VideoForge.Api/VideoForge.Api.csproj", "src/VideoForge.Api/"]
RUN dotnet restore "src/VideoForge.Api/VideoForge.Api.csproj"

COPY ["src/VideoForge.Api/", "src/VideoForge.Api/"]
RUN dotnet publish "src/VideoForge.Api/VideoForge.Api.csproj" -c Release -o /app/publish /p:UseAppHost=false

# ffmpeg/ffprobe live in Ubuntu universe; jammy ships a recent enough build.
FROM mcr.microsoft.com/dotnet/aspnet:8.0-jammy AS final
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=build /app/publish ./

ENV ASPNETCORE_URLS=http://+:8080 \
    DOTNET_RUNNING_IN_CONTAINER=true \
    ConnectionStrings__Postgres="Host=db;Port=5432;Database=videoforge;Username=videoforge;Password=videoforge" \
    VideoForge__DataDirectory="/var/lib/videoforge"

EXPOSE 8080
VOLUME ["/var/lib/videoforge"]

ENTRYPOINT ["dotnet", "VideoForge.Api.dll"]
