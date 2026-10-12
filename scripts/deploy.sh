#!/usr/bin/env sh
# Mise à jour sur le VPS :  ./scripts/deploy.sh
set -eu
cd "$(dirname "$0")/.."
git pull --ff-only
[ -f .env ] || { echo "Créez d'abord .env (cp .env.example .env)"; exit 1; }
mkdir -p data && chown 1000:1000 data 2>/dev/null || sudo chown 1000:1000 data
if grep -q "^USE_CADDY=1" .env; then docker compose --profile caddy up -d --build; else docker compose up -d --build; fi
docker compose ps
