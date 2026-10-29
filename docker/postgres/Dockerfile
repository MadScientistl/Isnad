# postgres:18 + PostGIS (the official postgis/postgis image has no arm64 build).
FROM postgres:18
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-18-postgis-3 \
 && rm -rf /var/lib/apt/lists/*
