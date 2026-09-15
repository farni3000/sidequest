.PHONY: install dev test lint typecheck check up down start brouter-up brouter-segments

install:
	cd backend && uv sync --extra dev

dev:
	cd backend && uv run uvicorn app.main:app --reload

test:
	cd backend && uv run pytest

lint:
	cd backend && uv run ruff check app tests

typecheck:
	cd backend && uv run mypy app

check: lint typecheck test

brouter-segments:
	./brouter/setup_segments.sh ardennes

brouter-up:
	docker compose up -d brouter

up:
	docker compose up --build

start:
	./start.sh

down:
	docker compose down
