# StockFlow developer and CI entry points (owner: devops).
#
# Every gate threshold and scanner flag comes from config/quality-policy.yaml through
# `scripts/quality_policy.py get|args`; nothing is typed here (`make policy-check` fails on drift).
# `tee` is used to keep reports, so pipefail is mandatory (docs/SECURITY.md section 9).

SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help
export PATH := $(HOME)/.local/bin:$(PATH)

UV ?= uv
POLICY := $(UV) run python scripts/quality_policy.py

# Docker needs sudo on the dev VM; use plain docker wherever the daemon is reachable.
# Override with `make docker-test DOCKER=docker`.
ifneq ($(filter docker-% compose-% trivy-image%,$(MAKECMDGOALS)),)
SUDO ?= $(shell docker info >/dev/null 2>&1 || echo sudo)
endif
SUDO ?=
DOCKER ?= $(strip $(SUDO) docker)

IMAGE ?= stockflow:local
CONTAINER ?= stockflow-local
HOST_PORT ?= 8000
PLATFORM = $(shell $(POLICY) get trivy.image.platform)
APP_VERSION ?= $(shell $(UV) run python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])")
# 40 zeros when git has no commit yet (the smoke test refuses the literal "unknown").
GIT_SHA ?= $(shell git rev-parse --verify --quiet HEAD || echo 0000000000000000000000000000000000000000)
BUILD_TIME ?= $(shell date -u +%Y-%m-%dT%H:%M:%SZ)
# Optional OCI source label (CI sets it so the GHCR package links back to the repository).
IMAGE_SOURCE ?=
IMAGE_TAR ?= reports/image.tar
# `docker save --platform` is needed on the arm64 dev VM (containerd image store, foreign-platform image;
# SECURITY.md section 9). CI builds natively and overrides this with an empty value.
SAVE_PLATFORM_FLAG ?= --platform $(PLATFORM)

SCENARIO ?=
# Extra flags for demo/inject.py and demo/reset.py, e.g. DEMO_ARGS=--allow-runtime-failures (runtime scenarios)
DEMO_ARGS ?=
BASE_URL ?= http://localhost:8000
EXPECT_SHA ?=

.PHONY: help sync run lock-check policy-check workflow-lint tools \
	lint format-check format typecheck test test-unit test-integration test-coverage \
	security security-bandit security-audit security-trivy-fs security-trivy-config quality \
	docker-build docker-run docker-start docker-stop docker-test docker-guard docker-save \
	docker-scan trivy-image-tar compose-up compose-down smoke perf \
	demo-list demo demo-reset demo-status demo-fixtures demo-image-guard demo-verify demo-verify-all clean-reports

help: ## List the targets
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/^([a-zA-Z0-9_-]+):.*## (.*)/  \1\t\2/' | column -t -s $$'\t'

reports:
	mkdir -p reports

clean-reports: ## Remove generated reports/
	rm -rf reports

# ---------------------------------------------------------------- environment
sync: ## Install dependencies from uv.lock (uv sync --frozen)
	$(UV) sync --frozen

run: ## Run the app locally on $PORT (default 8000)
	$(UV) run python -m app

tools: ## Install pinned, checksum-verified trivy, actionlint and k6 into ~/.local/bin
	scripts/deploy/install_tool.sh trivy
	scripts/deploy/install_tool.sh actionlint
	scripts/deploy/install_tool.sh k6

# ---------------------------------------------------------------- configuration gates
lock-check: ## uv.lock matches pyproject.toml
	$(UV) lock --check

policy-check: ## Validate the quality policy, its mirrors, workflows and drift scan
	$(POLICY) check

workflow-lint: ## actionlint over .github/workflows (install with `make tools`)
	actionlint .github/workflows/*.yml

# ---------------------------------------------------------------- code quality gates
lint: ## ruff check
	$(UV) run ruff check .

format-check: ## ruff format --check
	$(UV) run ruff format --check .

format: ## Apply ruff formatting (not a gate)
	$(UV) run ruff format .

typecheck: ## mypy (strict; files come from pyproject.toml)
	$(UV) run mypy

test: ## pytest (all tests)
	$(UV) run pytest

test-unit: reports ## pytest, everything that is not marked integration
	$(UV) run pytest -m "not integration" --junitxml=reports/junit-unit.xml

test-integration: reports ## pytest, tests marked integration
	$(UV) run pytest -m integration --junitxml=reports/junit-integration.xml

test-coverage: reports ## pytest with branch coverage; fails under coverage.fail_under (policy)
	$(UV) run pytest --cov --cov-report=term-missing --cov-report=xml:reports/coverage.xml \
		--cov-fail-under=$$($(POLICY) get coverage.fail_under) \
		--junitxml=reports/junit-coverage.xml | tee reports/pytest-coverage.txt

# ---------------------------------------------------------------- security gates (flags from policy)
security-bandit: reports ## bandit SAST
	$(UV) run bandit $$($(POLICY) args bandit) | tee reports/bandit.txt

security-audit: reports ## pip-audit over the exported lock
	$(UV) export $$($(POLICY) args uv-export)
	$(UV) run pip-audit $$($(POLICY) args pip-audit) | tee reports/pip-audit.txt

security-trivy-fs: reports ## trivy filesystem scan (vuln + secret)
	trivy fs $$($(POLICY) args trivy-fs) . | tee reports/trivy-fs.txt

security-trivy-config: reports ## trivy config scan (Dockerfile, compose, workflows)
	trivy config $$($(POLICY) args trivy-config) . | tee reports/trivy-config.txt

security: policy-check security-bandit security-audit security-trivy-fs security-trivy-config ## All source-level security gates

quality: lint format-check typecheck test-coverage security ## Everything that must be green before a merge

# ---------------------------------------------------------------- container
docker-build: ## Build the linux/amd64 image (IMAGE=stockflow:local), loaded into the local daemon
	$(DOCKER) buildx build --platform $(PLATFORM) --load -t $(IMAGE) \
		--build-arg APP_VERSION=$(APP_VERSION) --build-arg GIT_SHA=$(GIT_SHA) --build-arg BUILD_TIME=$(BUILD_TIME) \
		$(if $(IMAGE_SOURCE),--label org.opencontainers.image.source=$(IMAGE_SOURCE)) .

docker-run: ## Run the image in the foreground on http://localhost:$(HOST_PORT)
	$(DOCKER) run --rm --name $(CONTAINER) --platform $(PLATFORM) -p $(HOST_PORT):8000 $(IMAGE)

docker-start: ## Run the image detached
	$(DOCKER) run -d --name $(CONTAINER) --platform $(PLATFORM) -p $(HOST_PORT):8000 $(IMAGE)

docker-stop: ## Stop and remove the detached container
	$(DOCKER) rm -f $(CONTAINER)

docker-guard: ## Assert the image holds no demo/test material, runs non-root, has no build tools
	$(DOCKER) run --rm -i --platform $(PLATFORM) --entrypoint sh $(IMAGE) -s < scripts/deploy/image_guard.sh

docker-test: docker-build ## Build, run, smoke test (version == baked sha), image guard, graceful stop
	DOCKER="$(DOCKER)" PLATFORM="$(PLATFORM)" scripts/deploy/docker_test.sh $(IMAGE) $(GIT_SHA)

docker-save: reports ## Save the image to $(IMAGE_TAR) for trivy --input
	$(DOCKER) save $(SAVE_PLATFORM_FLAG) $(IMAGE) > $(IMAGE_TAR)

# Order matters: the non-blocking full report (includes unfixed findings) is written FIRST so it exists as an
# artifact even when the blocking gate below fails the target.
trivy-image-tar: reports ## Non-blocking full report, then the blocking trivy image gate, on $(IMAGE_TAR)
	trivy image $$($(POLICY) args trivy-image-report) --input $(IMAGE_TAR) --format json --output reports/trivy-image-full.json
	trivy image $$($(POLICY) args trivy-image) --input $(IMAGE_TAR) | tee reports/trivy-image.txt

docker-scan: docker-save trivy-image-tar ## Save the image and run the trivy image gate

compose-up: ## docker compose up --build (local)
	$(strip $(SUDO) env) APP_VERSION=$(APP_VERSION) GIT_SHA=$(GIT_SHA) BUILD_TIME=$(BUILD_TIME) HOST_PORT=$(HOST_PORT) docker compose up --build -d

compose-down: ## docker compose down
	$(DOCKER) compose down

# ---------------------------------------------------------------- post-deploy checks
smoke: ## Smoke test a running instance: make smoke BASE_URL=https://... [EXPECT_SHA=<sha>]
	$(UV) run python scripts/smoke_test.py --base-url $(BASE_URL) $(if $(EXPECT_SHA),--expect-sha $(EXPECT_SHA))

perf: reports ## k6 load test against BASE_URL (writes data; opt-in). Shape and thresholds from policy
	k6 run $$($(POLICY) args k6) -e BASE_URL=$(BASE_URL) --summary-export reports/k6-summary.json load-tests/performance.js

# ---------------------------------------------------------------- defect injection (demo)
demo-list: ## List the defect scenarios
	$(UV) run python -m demo list

demo-status: ## Show the active scenario and file states
	$(UV) run python -m demo status

demo: ## Inject a defect: make demo SCENARIO=<id> [DEMO_ARGS=--allow-runtime-failures]
	@test -n "$(SCENARIO)" || { echo "usage: make demo SCENARIO=<id> [DEMO_ARGS=...]  (see make demo-list)"; exit 2; }
	$(UV) run python demo/inject.py $(SCENARIO) $(DEMO_ARGS)

demo-reset: ## Restore the clean baseline [DEMO_ARGS=--force to discard user edits]
	$(UV) run python demo/reset.py $(DEMO_ARGS)

demo-fixtures: ## Re-check that the demo fixtures still trigger the real scanners (needs network)
	$(UV) run python demo/inject.py --validate-fixtures

demo-image-guard: ## The demo engine's own image guard on $(IMAGE) (also part of docker-test)
	$(UV) run python -m demo image-guard $(IMAGE)

demo-verify: ## Prove a scenario is caught by its real gate: make demo-verify SCENARIO=<id>
	@test -n "$(SCENARIO)" || { echo "usage: make demo-verify SCENARIO=<id>"; exit 2; }
	$(UV) run python -m demo verify $(SCENARIO)

demo-verify-all: ## Verify every scenario
	$(UV) run python -m demo verify-all
