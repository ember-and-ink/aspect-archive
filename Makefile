.PHONY: help compile test test-js run docker-build docker-up docker-pull docker-up-prod import gallery-ids check names thumbs settings-import \
	frontend-install frontend-lint frontend-typing frontend-test frontend-build frontend-dev frontend-smoke api-schema \
	bump-minor bump-major pre-commit-install

# Every target reads .env (see .env.template) via aspect/config.py -- most
# importantly ASPECT_ARCHIVE_DIR, the cards folder they all read and write.

# `make` on its own prints this. A target's one-line summary is the `##` comment
# on the target's own line, and `##@` starts a section; the longer comments above
# the targets are for whoever is reading the file and are not printed.
.DEFAULT_GOAL := help

help:
	@echo "Usage: make <target> [ARGS=...]"
	@awk 'BEGIN {FS = ":.*##"} \
		/^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5); next } \
		/^[a-zA-Z0-9_-]+:.*##/ { printf "  \033[36m%-17s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""

##@ Build and test

# Concatenate userscript/src_jai/*.js   -> userscript/jai-proxy-bridge.user.js
# and userscript/src_saucepan/*.js       -> userscript/saucepan-proxy-bridge.user.js
compile: ## Build the userscripts from userscript/src_*/
	uv run python scripts/compile_userscript_jai.py
	uv run python scripts/compile_userscript_saucepan.py

# Run the Python test suite
test: ## Run the Python test suite
	uv run python -m pytest -q

# Run the userscripts' unit tests (node:test, no deps). Resolves its fixtures
# relative to userscript/, hence the cd. The browser client has its own suite --
# `make frontend-test`, vitest, below.
test-js: ## Run the userscript unit tests (node:test)
	cd userscript && node --test

# Once per checkout. The hooks in .pre-commit-config.yaml run the frontend
# lint/format/typing checks CI runs, plus generic hygiene and gitleaks.
pre-commit-install: ## Install the pre-commit git hooks
	uv run pre-commit install

run: ## Start the server on :8000
	uv run python -m aspect.server

# Patch releases need nothing: the publish workflow assigns the patch number
# from the git tags on every merge to main. These raise the version in
# pyproject.toml (and uv.lock), which makes the next release start a new minor
# (0.1.x -> 0.2.0) or major (0.1.x -> 1.0.0) series.
bump-minor: ## Raise the minor version in pyproject.toml
	uv version --bump minor

bump-major: ## Raise the major version in pyproject.toml
	uv version --bump major

##@ Browser client

# ---------------------------------------------------------------------------
# frontend/ -- the browser client (docs/UI_REWRITE_PLAN.md). Served by Vite on
# :5173 in dev and from frontend/dist by the server itself otherwise, at the
# root. It replaced the vendored web/ frontend at the Stage 7 cut-over; that
# directory lives on the `legacy-web` branch now.
# ---------------------------------------------------------------------------

frontend-install: ## Install the browser client deps (npm ci)
	cd frontend && npm ci

frontend-lint: ## Format and lint the browser client
	cd frontend && npm run format && npm run lint

frontend-typing: ## Type-check the browser client
	cd frontend && npx tsc -b --noEmit

frontend-test: ## Run the browser client tests (vitest)
	cd frontend && npm run test

frontend-build: ## Build frontend/dist, which the server serves
	cd frontend && npm run build

# The browser gate: drives the real app against a running server (`make run`)
# and fails on any console error, any failed request to our own origin, or any
# broken thumbnail. Not part of `make test` -- it needs a server and a browser.
# Playwright lives outside uv here; see docs/UI_REWRITE_PLAN.md Stage 1.
frontend-smoke: ## Browser gate against a running server
	python frontend/tests/smoke.py http://127.0.0.1:8000 make

# The client dev server. `make run` in another terminal supplies the API --
# vite.config.ts proxies /api, /proxy, /health and /existing to :8000.
frontend-dev: ## Run the Vite dev server on :5173
	cd frontend && npm run dev

# Regenerate the typed client from FastAPI. The intermediate openapi.json is
# gitignored; src/lib/api-schema.ts is committed, and CI re-runs this target and
# fails on a diff -- that check is what keeps the client honest about the
# routes, so an API change is a schema change first and a UI change second.
api-schema: ## Regenerate the typed client from FastAPI
	uv run python -m scripts.export_openapi_schema
	cd frontend && npx openapi-typescript openapi.json -o src/lib/api-schema.ts


##@ Container

# The same server in a container: one image, one mount (./data), one port.
# The targets below still run on the host, against that same ./data.
docker-build: ## Build the container image locally
	docker compose build

docker-up: ## Start the locally built container
	docker compose up -d

# The published image instead of a local build -- what a server runs. Defaults
# in compose.prod.yaml target unraid (/mnt/user/appdata/aspect-archive, uid 99:100);
# see docs/DEPLOY.md. Useful on the Mac too, to check what :latest actually
# does before the server pulls it.
docker-pull: ## Pull the published image (compose.prod.yaml)
	docker compose -f compose.prod.yaml pull

docker-up-prod: ## Start the published image
	docker compose -f compose.prod.yaml up -d

##@ Archive maintenance

# Bulk-import card PNGs from ./import into the cards folder -- datacat, JannyAI
# and Chub.ai exports are auto-detected (see scripts/import_cards.py). Cards
# already on disk are skipped, never overwritten. The same run also sweeps the
# cards folder for orphans (cards dropped in by hand, with no extensions.jai
# stamp), imports them in place and retires the originals to state/orphans.
# Extra flags pass through via ARGS, e.g. `make import ARGS=--no-compress`.
# The server never rescans the archive on its own, so a running one cannot see
# what these scripts write until the top bar's Rescan button is pressed.
RESCAN_HINT = @echo "If cards were written, press Rescan in the UI (or POST /api/v1/refresh) -- a running server won't see them until then."

import: ## Bulk-import card PNGs from ./import
	uv run python scripts/import_cards.py $(ARGS) --fetch-datacat-images
	$(RESCAN_HINT)

# Backfill `extensions.gallery_id` (SillyTavern-CharacterLibrary's per-character
# gallery handle) into any card missing one. Read-only report by default;
# `make gallery-ids ARGS=--apply` writes them in place.
gallery-ids: ## Backfill extensions.gallery_id (ARGS=--apply)
	uv run python scripts/backfill_gallery_ids.py $(ARGS)
	$(RESCAN_HINT)

datacat-ids: ## Backfill extensions.datacat ids (ARGS=--apply)
	uv run python scripts/backfill_datacat_ids.py $(ARGS)
	$(RESCAN_HINT)

# Re-audit built cards against the current macro/formatting rules. Read-only;
# `make check ARGS=--repair` rewrites the cards that would change.
check: ## Re-audit built cards (ARGS=--repair)
	uv run python scripts/check_cards.py $(ARGS)
	$(RESCAN_HINT)

# Find card names that aren't names -- a tagline welded on (`Mia, your desperate
# roommate`) or a generic placeholder hiding the real character (`Narrator`).
# SillyTavern feeds data.name to the model as "You are <name>", so both wreck
# the roleplay. Read-only report by default; `make names ARGS=--interactive`
# walks the findings and applies the renames you confirm. Nothing is ever
# renamed automatically -- the suggestions are ~87% right, not 100%.
# Interactive decisions are appended to logs/name_repair.jsonl (diagnosis +
# what you actually chose), which is the ground truth for improving the rules:
# `make names ARGS=--stats` scores them against it.
names: ## Find card names that are not names (ARGS=--interactive)
	uv run python scripts/fix_names.py $(ARGS)
	$(RESCAN_HINT)

# Tidy the browse grid's thumbnail cache (data/cache/thumbs/avatar): render the
# cards that have no thumb, retire the thumbs whose card is gone, and fix the
# ones whose name differs from their card's only by case -- macOS resolves those
# anyway, so they look fine here and would silently miss in a Linux container.
# The API also generates on miss, so this only ever moves work off first paint.
# Read-only report by default; `make thumbs ARGS=--apply` writes.
thumbs: ## Tidy the browse thumbnail cache (ARGS=--apply)
	uv run python scripts/sync_thumbs.py $(ARGS)

# Seed data/settings.json from an existing SillyTavern install: lifts the
# Character Library blob (provider tokens, followed creators, display prefs)
# out of its settings.json so the standalone browser stops depending on
# origin-keyed browser storage -- which silently held a copy left behind by
# SillyTavern itself, since its stock port is also 8000.
# Read-only report by default; `make settings-import ARGS=--apply` writes.
settings-import: ## Seed data/settings.json from SillyTavern (ARGS=--apply)
	uv run python scripts/import_st_settings.py $(ARGS)
