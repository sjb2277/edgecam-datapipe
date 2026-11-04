PROFILE ?= core
# .env가 있으면 값을 export해서 쓰고, 없으면 코드 기본값(.env.example과 동일)을 쓴다
LOADENV := set -a && { [ -f .env ] && . ./.env || true; } && set +a &&
RUN_ID ?= d02-$(shell date +%Y%m%dT%H%M%S)
MODE ?= clip
DURATION ?= 60
SCHEDULE ?=
COMPOSE := docker compose -f docker/compose.yaml --env-file .env

.PHONY: up down test test-integration lint schemas capture-recv capture-send match knobs \
	link-up link-down link-status link-apply link-recv link-send link-run

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
	$(LOADENV) uv run python -m pipe.capture.sender --run-id $(RUN_ID) --mode $(MODE) --duration $(DURATION) --schedule "$(SCHEDULE)" $(if $(HOST),--host $(HOST))

match:
	$(LOADENV) uv run python -m pipe.capture.match --run-id $(RUN_ID)

knobs:
	$(LOADENV) uv run python -m pipe.capture.knobs --run-id $(RUN_ID)

# 링크 계층 (D03): ue/edge netns + veth + netem(ue→edge). sudo 필요, 호스트 netns는 건드리지 않는다.
# 이름·주소는 .env(LINK_*)에서 읽고 없으면 아래 기본값을 쓴다. up/down/apply 모두 재실행해도 안전하다.
# 준비: sudo -v && make link-up   /   정리: make link-down
#
# (A) 한 터미널 (권장): link-run은 시나리오 "이름"을 받는다(scenarios/<이름>.yaml). 자세한 내용은 link-run 위 주석.
#   make link-run SCENARIO=delay [DURATION=60] [RUN_ID=r1]
#
# (B) 터미널 3개 (수동): link-apply는 시나리오 "경로"를 받는다.
#   터미널1: make link-apply SCENARIO=scenarios/delay.yaml RUN_ID=r1 DURATION=60
#   터미널2: make link-recv RUN_ID=r1 DURATION=60     (edge, 사용자 권한)
#   터미널3: make link-send RUN_ID=r1 DURATION=60     (ue, 사용자 권한)
#   이후:    make match RUN_ID=r1
LINKENV := $(LOADENV) UE=$${LINK_NS_UE:-ue}; EDGE=$${LINK_NS_EDGE:-edge}; \
	DUE=$${LINK_DEV_UE:-veth-ue}; DEDGE=$${LINK_DEV_EDGE:-veth-edge}; \
	UEIP=$${LINK_UE_IP:-10.250.0.1}; EDGEIP=$${LINK_EDGE_IP:-10.250.0.2}; PFX=$${LINK_PREFIX_LEN:-24}; \
	has_ns() { ip netns list | awk '{print $$1}' | grep -qx "$$1"; };
# netns 안에서 원래 사용자로 실행(uv 캐시·data/runs가 root 소유가 되지 않게)
ASUSER = sudo -u $$(id -un) env HOME="$$HOME" PATH="$$PATH"

link-up:
	@$(LINKENV) set -e; \
	has_ns $$UE   || sudo ip netns add $$UE; \
	has_ns $$EDGE || sudo ip netns add $$EDGE; \
	sudo ip -n $$UE link show $$DUE >/dev/null 2>&1 || \
		sudo ip link add $$DUE netns $$UE type veth peer name $$DEDGE netns $$EDGE; \
	sudo ip -n $$UE   addr replace $$UEIP/$$PFX dev $$DUE; \
	sudo ip -n $$EDGE addr replace $$EDGEIP/$$PFX dev $$DEDGE; \
	sudo ip -n $$UE   link set lo up; sudo ip -n $$UE   link set $$DUE up; \
	sudo ip -n $$EDGE link set lo up; sudo ip -n $$EDGE link set $$DEDGE up; \
	sudo ip netns exec $$UE ping -c 3 -W 1 $$EDGEIP

link-down:
	@$(LINKENV) \
	if has_ns $$UE; then \
		sudo ip netns exec $$UE tc qdisc del dev $$DUE root 2>/dev/null || true; \
		sudo ip netns del $$UE; \
	fi; \
	if has_ns $$EDGE; then sudo ip netns del $$EDGE; fi; \
	ip netns list

link-status:
	@$(LINKENV) ip netns list; \
	if has_ns $$UE; then sudo ip -n $$UE -br addr; sudo ip netns exec $$UE tc -s qdisc show dev $$DUE; fi; \
	if has_ns $$EDGE; then sudo ip -n $$EDGE -br addr; fi

link-apply:
	$(LOADENV) uv run python -m pipe.link.runner --scenario $(SCENARIO) --run-id $(RUN_ID) --duration $$(( $(DURATION) + 30 ))

# $(MAKE)를 쓰지 않는다: make -n에서도 $(MAKE) 줄은 실제로 실행되어 sudo가 돌기 때문
link-recv:
	@$(LINKENV) sudo ip netns exec $$EDGE $(ASUSER) $(INNERENV) \
		uv run python -m pipe.capture.receiver --run-id $(RUN_ID) --duration $$(( $(DURATION) + 30 )) \
		$(RECV_GST_ARGS)

link-send:
	@$(LINKENV) sudo ip netns exec $$UE $(ASUSER) $(INNERENV) \
		uv run python -m pipe.capture.sender --run-id $(RUN_ID) --mode $(MODE) --duration $(DURATION) \
		--schedule "$(SCHEDULE)" --host $$EDGEIP

# 한 터미널 실행 (D03): recv(edge, 백그라운드) → apply(호스트, 백그라운드) → send(ue) → recv 종료 대기
#   → apply 종료(qdisc 삭제) → match. 먼저 같은 터미널에서 `sudo -v` 후 실행한다(sudo -n만 쓴다).
#   make link-run SCENARIO=delay [DURATION=60] [RUN_ID=...]   (RUN_ID 기본: d03-<시나리오>-<시각>)
#   수신기 GST 디버그 로그(선택): RECV_GST_DEBUG=rtpjitterbuffer:4,... [RECV_GST_DEBUG_MAX_BYTES=20000000]
#     → <run>/gst_debug.log (상한 초과 시 잘림, recv_meta.json의 gst_debug에 결과). 송신기에는 적용되지 않는다.
# 로그: <RUNS_DIR>/<RUN_ID>/{recv,link,send}.log
# 레시피에 $(MAKE)를 쓰지 않으므로 `make -n link-run`은 명령을 출력만 하고 sudo를 실행하지 않는다.
LINK_RUN_ID = $(if $(filter file,$(origin RUN_ID)),d03-$(SCENARIO)-$(shell date +%Y%m%dT%H%M%S),$(RUN_ID))
RECV_GST_DEBUG ?=
RECV_GST_DEBUG_MAX_BYTES ?= 20000000
RECV_GST_ARGS = $(if $(RECV_GST_DEBUG),--gst-debug '$(RECV_GST_DEBUG)' --gst-debug-max-bytes $(RECV_GST_DEBUG_MAX_BYTES))
# sudo가 환경을 초기화하므로 netns 안 프로세스는 .env를 다시 읽는다
INNERENV = bash -c 'set -a; [ -f .env ] && . ./.env; set +a; exec "$$@"' _

link-run:
	@$(LINKENV) set -u; \
	SCN=scenarios/$(SCENARIO).yaml; RID=$(LINK_RUN_ID); RD=$${RUNS_DIR:-data/runs}/$$RID; \
	{ [ -n "$(SCENARIO)" ] && [ -f "$$SCN" ]; } || { echo "[link-run] SCENARIO=<이름> 필요 ($$SCN 없음)"; exit 2; }; \
	sudo -n true 2>/dev/null || { echo "[link-run] sudo 자격 캐시 없음: 먼저 sudo -v"; exit 2; }; \
	{ has_ns $$UE && has_ns $$EDGE; } || { echo "[link-run] netns $$UE/$$EDGE 없음: make link-up"; exit 2; }; \
	mkdir -p "$$RD"; RPID=; APID=; \
	cleanup() { \
		[ -n "$$APID" ] && kill -TERM $$APID 2>/dev/null; \
		[ -n "$$RPID" ] && kill -TERM $$RPID 2>/dev/null; \
		sudo -n ip netns exec $$UE tc qdisc del dev $$DUE root 2>/dev/null; \
		true; \
	}; \
	trap cleanup EXIT; trap 'exit 130' INT TERM; \
	wait_log() { i=0; while ! grep -q "$$2" "$$1" 2>/dev/null; do \
		i=$$((i+1)); [ $$i -gt 75 ] && { echo "[link-run] 대기 시간 초과: $$1"; tail -5 "$$1"; exit 1; }; \
		sleep 0.2; done; }; \
	echo "[link-run] run=$$RID scenario=$$SCN mode=$(MODE) duration=$(DURATION)s dir=$$RD"; \
	$(if $(RECV_GST_DEBUG),echo "[link-run] recv gst debug=$(RECV_GST_DEBUG) max=$(RECV_GST_DEBUG_MAX_BYTES)B";) \
	sudo -n ip netns exec $$EDGE $(ASUSER) $(INNERENV) \
		uv run python -m pipe.capture.receiver --run-id $$RID --duration $$(( $(DURATION) + 30 )) \
		$(RECV_GST_ARGS) > "$$RD/recv.log" 2>&1 & RPID=$$!; \
	wait_log "$$RD/recv.log" READY; grep READY "$$RD/recv.log"; \
	uv run python -m pipe.link.runner --scenario $$SCN --run-id $$RID --duration $$(( $(DURATION) + 30 )) \
		> "$$RD/link.log" 2>&1 & APID=$$!; \
	wait_log "$$RD/link.log" "seg=0"; head -1 "$$RD/link.log"; \
	sudo -n ip netns exec $$UE $(ASUSER) $(INNERENV) \
		uv run python -m pipe.capture.sender --run-id $$RID --mode $(MODE) --duration $(DURATION) \
		--schedule "$(SCHEDULE)" --host $$EDGEIP > "$$RD/send.log" 2>&1; SRC=$$?; \
	tail -1 "$$RD/send.log"; \
	wait $$RPID; RRC=$$?; RPID=; tail -1 "$$RD/recv.log"; \
	kill -TERM $$APID 2>/dev/null; wait $$APID; ARC=$$?; APID=; tail -1 "$$RD/link.log"; \
	echo "[link-run] exit send=$$SRC recv=$$RRC link=$$ARC"; \
	uv run python -m pipe.capture.match --run-id $$RID; \
	[ $$SRC -eq 0 ] && [ $$RRC -eq 0 ]
