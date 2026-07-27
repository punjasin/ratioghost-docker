# Ratio Ghost seeder (Bun). Build context = repo root:
#   docker build -f bun/Dockerfile -t ratioghost-seeder .
#   docker run --rm -v "$PWD/torrents:/app/torrents" ratioghost-seeder
FROM oven/bun:1

WORKDIR /app

COPY bun/package.json /app/package.json
COPY bun/seeder.ts /app/seeder.ts

# Drop .torrent files here (bind-mount a host folder over it).
RUN mkdir -p /app/torrents
ENV RG_TORRENTS_DIR=/app/torrents \
    RG_INTERVAL_MIN=30 \
    RG_PORT=6881

CMD ["bun", "run", "seeder.ts"]
