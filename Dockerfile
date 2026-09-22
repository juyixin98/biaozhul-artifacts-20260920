FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY VideoForge.sln ./
COPY src/VideoForge.Api/VideoForge.Api.csproj src/VideoForge.Api/
RUN dotnet restore src/VideoForge.Api/VideoForge.Api.csproj
COPY src/ src/
RUN dotnet publish src/VideoForge.Api/VideoForge.Api.csproj -c Release -o /app/publish --no-restore

FROM mcr.microsoft.com/dotnet/aspnet:8.0 AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app/publish .
ENV ASPNETCORE_URLS=http://+:8080
EXPOSE 8080
ENTRYPOINT ["dotnet", "VideoForge.Api.dll"]
