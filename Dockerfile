# CacheLens WebMCP demo image.
#
# Two things about this build are deliberate and worth not "tidying" later:
#
# 1. The install is editable (`pip install -e .`). cachelens/server.py locates
#    its traces and page with `Path(__file__).resolve().parents[2]`, which is
#    the repo root under an editable install at /app, but would resolve to
#    site-packages' parent under a regular install -- and the trace catalog
#    would then point at a directory that does not exist. Editable install is
#    what keeps that path correct without changing the code.
#
# 2. Only what `cachelens serve` actually reads is copied: the package, the
#    page, and the bundled traces. The generated example fixtures
#    (examples/*.jsonl, written by gen_fixtures.py) are not needed -- the
#    server's catalog reads examples/real-agents/traces/ exclusively.
#
# No dependencies are installed because there are none: cachelens is stdlib
# only. That is also why this image is small and starts in well under a second.

FROM python:3.12-slim

# Unbuffered so Fly's log stream shows request output as it happens.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# pyproject declares readme = "README.md", so the build fails without it.
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY web/ ./web/
COPY examples/real-agents/traces/ ./examples/real-agents/traces/

RUN pip install --no-cache-dir -e . \
 && python -c "from cachelens.server import TRACE_DIR, WEB_DIR, default_catalog; \
assert TRACE_DIR.is_dir(), TRACE_DIR; \
assert (WEB_DIR / 'index.html').is_file(), WEB_DIR; \
ids = default_catalog().ids; \
assert ids, 'no traces baked into the image'; \
print('image self-check ok:', len(ids), 'traces', ids)"

# Read-only service, no writes anywhere; run unprivileged.
RUN useradd --create-home --shell /usr/sbin/nologin cachelens
USER cachelens

EXPOSE 8080

# The existing CLI command, unchanged. 0.0.0.0 so the Fly proxy can reach it.
CMD ["cachelens", "serve", "--host", "0.0.0.0", "--port", "8080"]
