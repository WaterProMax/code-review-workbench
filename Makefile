# One-command demo. Invoke from anywhere: make -C <Test2> demo

ROOT := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
COMPOSE := docker compose -f "$(ROOT)/docker-compose.yml" --project-directory "$(ROOT)"

.PHONY: demo down

demo:
	@set -eu; \
	if ! command -v docker >/dev/null 2>&1; then \
		printf '%s\n' "error: docker was not found on PATH." "Install Docker Desktop or Docker Engine, then run 'make demo' again. Nothing was started." >&2; \
		exit 1; \
	fi; \
	if ! docker compose version >/dev/null 2>&1; then \
		printf '%s\n' "error: 'docker compose' is not available." "Install the Docker Compose v2 plugin, then run 'make demo' again. Nothing was started." >&2; \
		exit 1; \
	fi; \
	if ! docker info >/dev/null 2>&1; then \
		printf '%s\n' "error: the Docker daemon is not running, or this user cannot access it." "Start Docker, then run 'make demo' again. Nothing was started." >&2; \
		exit 1; \
	fi; \
	if ! $(COMPOSE) up --build -d --wait; then \
		printf '%s\n' "error: the demo stack did not become healthy." "Logs: docker compose -f \"$(ROOT)/docker-compose.yml\" --project-directory \"$(ROOT)\" logs" "Stop: make -C \"$(ROOT)\" down" >&2; \
		exit 1; \
	fi; \
	printf '%s\n' "" "Workbench:  http://localhost:8080" "API health: http://localhost:8000/api/health" "Stop:       make down"

down:
	@set -eu; \
	if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then \
		printf '%s\n' "error: docker compose is not available. The stack was not stopped because Docker cannot be invoked." >&2; \
		exit 1; \
	fi; \
	if ! docker info >/dev/null 2>&1; then \
		printf '%s\n' "error: the Docker daemon is not running. Start Docker, then run 'make down' again. Nothing was stopped." >&2; \
		exit 1; \
	fi; \
	$(COMPOSE) down --remove-orphans
