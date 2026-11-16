# Offline runner for agent-generated parsers (ADR-0004 revised).
# Contains only the pure parsing stack. Run with:
#   docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL \
#     --security-opt no-new-privileges --memory 1g --pids-limit 128 \
#     -v <case_dir>:/work:ro scrapeline-sandbox:latest
FROM python:3.12.8-slim

RUN pip install --no-cache-dir \
      "pydantic>=2.10,<3" "selectolax>=1.0" "beautifulsoup4>=4.13" \
      "python-dateutil>=2.9" "pyyaml>=6.0"

# Only pure modules (no fetch/, no store/, no settings). Harness grows in step 4.
COPY src/scrapeline/__init__.py src/scrapeline/errors.py /opt/sl/scrapeline/
COPY src/scrapeline/agents/sandbox_harness.py /opt/sl/harness.py

ENV PYTHONPATH=/opt/sl PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --uid 10001 --no-create-home sandbox
USER sandbox
WORKDIR /tmp
ENTRYPOINT ["python", "-I", "-S", "/opt/sl/harness.py"]
