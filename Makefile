PROFILE ?= core
# .env가 있으면 값을 export해서 쓰고, 없으면 코드 기본값(.env.example과 동일)을 쓴다
LOADENV := set -a && { [ -f .env ] && . ./.env || true; } && set +a &&
RUN_ID ?= d02-$(shell date +%Y%m%dT%H%M%S)
MODE ?= clip
DURATION ?= 60
SCHEDULE ?=
COMPOSE := docker compose -f docker/compose.yaml --env-file .env

.PHONY: up down test test-integration lint schemas capture-recv capture-send match knobs

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

# 캡처 송수신 (D02). 수신기를 먼저 띄운 뒤 같은 RUN_ID로 송신한다.
#   터미널1: make capture-recv RUN_ID=r1
#   터미널2: make capture-send RUN_ID=r1 MODE=clip DURATION=60 [SCHEDULE="20:bitrate=500"]
#   이후:    make match RUN_ID=r1   /   make knobs RUN_ID=r1
capture-recv:
	$(LOADENV) uv run python -m pipe.capture.receiver --run-id $(RUN_ID) --duration $$(( $(DURATION) + 30 ))

capture-send:
	$(LOADENV) uv run python -m pipe.capture.sender --run-id $(RUN_ID) --mode $(MODE) --duration $(DURATION) --schedule "$(SCHEDULE)"

match:
	$(LOADENV) uv run python -m pipe.capture.match --run-id $(RUN_ID)

knobs:
	$(LOADENV) uv run python -m pipe.capture.knobs --run-id $(RUN_ID)
