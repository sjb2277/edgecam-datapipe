PROFILE ?= core
COMPOSE := docker compose -f docker/compose.yaml --env-file .env

.PHONY: up down test test-integration lint schemas

up:
	$(COMPOSE) --profile $(PROFILE) up -d --wait

# 볼륨은 유지한다 (삭제하려면 직접 `docker compose ... down -v`)
down:
	$(COMPOSE) --profile '*' down

test:
	uv run pytest -m "not integration"

# .env 값을 export해서 실행한다 (core 프로파일 기동 필요)
test-integration:
	set -a && . ./.env && set +a && uv run pytest -m integration

lint:
	uv run ruff check .
	uv run ruff format --check .

schemas:
	@echo "make schemas: D05에서 구현 예정"
