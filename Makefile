COMPOSE ?= docker compose
BUILDER ?= $(shell docker context show)
RUN = $(COMPOSE) run --rm --no-deps app
.PHONY: setup doctor data data-full data-raw data-ml test demo analyze-full a6-full a7-full b4-full b5-full technical-lag-full ml-full ml-train-full artifacts help
setup:
	@command -v docker >/dev/null || { echo 'Install Docker + Compose v2; see README Installation.'; exit 2; }
	@test -f .env || cp .env.example .env
	$(COMPOSE) build --builder "$(BUILDER)"
	$(RUN) python -m infra.distribution.runner doctor
doctor:
	$(RUN) python -m infra.distribution.runner doctor
data:
	$(RUN) python -m infra.distribution.runner data --mode demo
data-full:
	$(RUN) python -m infra.distribution.runner data --mode full
data-raw:
	$(RUN) python -m infra.distribution.runner data --mode raw
data-ml:
	$(RUN) python -m infra.distribution.runner data --mode ml
test:
	$(RUN) python -m unittest discover -s tests -v
	$(RUN) npm test --prefix processing/technical_lag
demo:
	$(RUN) python -m infra.distribution.runner demo
analyze-full:
	$(RUN) python -m infra.distribution.runner full --stage A6
	$(RUN) python -m infra.distribution.runner full --stage A7
	$(RUN) python -m infra.distribution.runner full --stage B4
	$(RUN) python -m infra.distribution.runner full --stage B5
a6-full:
	$(RUN) python -m infra.distribution.runner full --stage A6
a7-full:
	$(RUN) python -m infra.distribution.runner full --stage A7
b4-full:
	$(RUN) python -m infra.distribution.runner full --stage B4
b5-full:
	$(RUN) python -m infra.distribution.runner full --stage B5
technical-lag-full:
	$(RUN) python -m infra.distribution.runner full --stage technical-lag
ml-full:
	$(RUN) python -m infra.distribution.runner full --stage ML
ml-train-full:
	$(RUN) python -m infra.distribution.runner full --stage ML-train
artifacts: analyze-full ml-full
help:
	@echo 'setup | data | demo | test | data-full | analyze-full | ml-full | technical-lag-full | data-raw'
