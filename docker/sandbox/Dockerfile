# Offline runner for agent-generated parsers (ADR-0004 revised).
# Contains only the pure parsing stack. Run with:
#   docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL \
#     --security-opt no-new-privileges --memory 1g --pids-limit 128 \
#     -v <case_dir>:/work:ro scrapeline-sandbox:latest
FROM python:3.12.8-slim

# tzdata: the slim image has no zoneinfo database; source timezones need it offline.
RUN pip install --no-cache-dir \
      "pydantic>=2.10,<3" "selectolax>=1.0" "beautifulsoup4>=4.13" \
      "python-dateutil>=2.9" "pyyaml>=6.0" "tzdata>=2024.1"

# Only pure modules: no fetch/, store/, ops/, settings, DB or network code.
ARG PKG=/usr/local/lib/python3.12/site-packages/scrapeline
COPY src/scrapeline/__init__.py src/scrapeline/errors.py ${PKG}/
COPY src/scrapeline/pipeline/__init__.py src/scrapeline/pipeline/models.py src/scrapeline/pipeline/normalise.py src/scrapeline/pipeline/identity.py src/scrapeline/pipeline/locale.py src/scrapeline/pipeline/values.py ${PKG}/pipeline/
COPY src/scrapeline/catalog/__init__.py src/scrapeline/catalog/datasets.py ${PKG}/catalog/
COPY src/scrapeline/catalog/packs/ ${PKG}/catalog/packs/
COPY src/scrapeline/adapters/__init__.py src/scrapeline/adapters/rawdoc.py src/scrapeline/adapters/declarative.py ${PKG}/adapters/
COPY src/scrapeline/agents/sandbox_harness.py /opt/sl/harness.py

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --uid 10001 --no-create-home sandbox
USER sandbox
WORKDIR /tmp
ENTRYPOINT ["python", "-I", "/opt/sl/harness.py", "/work"]
