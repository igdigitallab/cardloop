.PHONY: test install-dev

# Запуск тестов через venv проекта
test:
	venv/bin/python -m pytest -q tests/

# Установка dev-зависимостей в venv
install-dev:
	venv/bin/pip install -r requirements-dev.txt
