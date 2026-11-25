.PHONY: setup check format lint types test isolated browser page cycle record-cycle correction curation live live-correction live-curation evidence evidence-rehearsal down

# The checks see no provider key and send no trace anywhere.
CLEAN = env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u OPENAI_API_KEY -u GEMINI_API_KEY \
	-u GOOGLE_API_KEY -u LANGSMITH_API_KEY -u LANGCHAIN_API_KEY \
	LANGSMITH_TRACING=false UV_OFFLINE=1
UV = uv run --frozen

setup:
	uv sync --frozen
	$(UV) playwright install chromium
	$(UV) lefthook install
	uv run --frozen --isolated --only-group server python -c ""
	uv run --frozen --isolated --only-group agent python -c ""
	uv run --frozen --isolated --no-default-groups python -c ""

check: format lint types test isolated browser
	@echo "make check passed"

format:
	$(CLEAN) $(UV) ruff format --check .

lint:
	$(CLEAN) $(UV) ruff check .

# Named, so that no configuration in a folder above this one outranks it.
types:
	$(CLEAN) $(UV) pyright --project pyproject.toml

test:
	$(CLEAN) $(UV) pytest -m "not browser" --cov --cov-report=term

# Each deployable imports with only its own dependencies.
isolated:
	$(CLEAN) $(UV) --isolated --only-group server python -c "import backend.main"
	$(CLEAN) $(UV) --isolated --only-group agent python -c "import triage_agent.entrypoint"
	$(CLEAN) $(UV) --isolated --only-group agent python -c "import curator.entrypoint"
	$(CLEAN) $(UV) --isolated --no-default-groups python -c "import cli.main"

browser:
	$(CLEAN) $(UV) pytest -m browser

# Starts the layer for the recorded cycle and leaves the page's button to press.
page:
	$(UV) python -m scripts.cycle --page

cycle:
	$(UV) python -m scripts.cycle

# The same cycle on the models .env names, written to recordings/ once every check holds.
record-cycle:
	$(UV) python -m scripts.cycle --record

correction:
	$(UV) python -m scripts.cycle --correction

curation:
	$(UV) python -m scripts.cycle --curation

live:
	$(UV) python -m scripts.cycle --live

live-correction:
	$(UV) python -m scripts.cycle --live --correction

live-curation:
	$(UV) python -m scripts.cycle --live --curation

# The five scenarios, three runs each on the models .env names, published in guide/.
evidence:
	$(UV) python -m scripts.evidence

# The same with no key, on scripted replies only, written under .local/evidence/.
evidence-rehearsal:
	$(UV) python -m scripts.evidence --rehearse

down:
	$(UV) python -m scripts.cycle --down
