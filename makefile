.PHONY: help install run start start-dev stop restart logs logs-dev shell build push push-dev clean check lint sort test coverage pr-ready

# Detect operating system
ifeq ($(OS),Windows_NT)
    # For Windows
    DATA_PATH := $(shell echo %cd%)\data
else
    # For Linux
    DATA_PATH := $(PWD)/data
endif

help:
	@echo "Comet Local Development Environment"
	@echo "-------------------------------------------------------------------------"
	@echo "install   : Install the required packages"
	@echo "run       : Run Comet"
	@echo "start     : Build and run the Comet container (requires Docker)"
	@echo "start-dev : Build and run the Comet container in development mode (requires Docker)"
	@echo "stop      : Stop and remove the Comet container (requires Docker)"
	@echo "logs      : Show the logs of the Comet container (requires Docker)"
	@echo "logs-dev  : Show the logs of the Comet container in development mode (requires Docker)"
	@echo "clean     : Remove all the temporary files"
	@echo "format    : Format the code using isort"
	@echo "lint      : Lint the code using ruff and isort"
	@echo "test      : Run the tests using pytest"
	@echo "coverage  : Run the tests and generate coverage report"
	@echo "pr-ready  : Run the linter and tests"
	@echo "-------------------------------------------------------------------------"
# Docker related commands

start: stop
	@docker compose -f docker-compose.yml up --build -d --force-recreate --remove-orphans
	@docker compose -f docker-compose.yml logs -f

start-dev: stop
	@docker compose -f docker-compose-dev.yml up --build -d --force-recreate --remove-orphans
	@docker compose -f docker-compose-dev.yml logs -f

stop:
	@docker compose -f docker-compose.yml down
	@docker compose -f docker-compose-dev.yml down

restart:
	@docker restart comet
	@docker logs -f comet

logs:
	@docker logs -f comet

logs-dev:
	@docker compose -f docker-compose-dev.yml logs -f

shell:
	@docker exec -it comet fish

build:
	@docker build -t comet .

push: build
	@docker tag comet:latest g0ldyy/comet:latest
	@docker push g0ldyy/comet:latest

push-dev: build
	@docker tag comet:latest g0ldyy/comet:dev
	@docker push g0ldyy/comet:dev

tidy:
	@docker rmi $(docker images | awk '$1 == "<none>" || $1 == "comet" {print $3}') -f


# Poetry related commands
clean:
	@find . -type f -name '*.pyc' -exec rm -f {} +
	@find . -type d -name '__pycache__' -exec rm -rf {} +
	@find . -type d -name '.pytest_cache' -exec rm -rf {} +
	@find . -type d -name '.ruff_cache' -exec rm -rf {} +

install:
	@poetry install --without dev --no-root

install-dev:
	@poetry install --with dev --no-root

# Run the application
run:
	@poetry run python comet/main.py

# Code quality commands
check:
	@poetry run pyright

lint:
	@poetry run ruff check comet
	@poetry run isort --check-only comet

sort:
	@poetry run isort comet

test:
	@poetry run pytest comet

coverage: clean
	@poetry run pytest comet --cov=comet --cov-report=xml --cov-report=term

# Run the linter and tests
pr-ready: clean lint test
