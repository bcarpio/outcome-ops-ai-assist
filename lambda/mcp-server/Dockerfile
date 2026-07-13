FROM python:3.12-slim

WORKDIR /app

# Build context is `lambda/` (not `lambda/mcp-server/`) so we can also
# copy lambda/shared/ for audit-writer access. See Makefile target
# `build-mcp-server-image`.
#
# requirements.txt is a hash-locked lockfile generated from
# requirements.in via `pip-compile --generate-hashes` (regenerate it
# there, don't hand-edit). --require-hashes verifies every wheel against
# the lock; --only-binary=:all: forbids sdists so pip can never execute
# a package's setup.py at build time.
COPY mcp-server/requirements.txt .
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.txt

COPY mcp-server/handler.py .
COPY mcp-server/session_store.py .
COPY mcp-server/sonarcloud.py .
COPY shared/ ./shared/

EXPOSE 8080

CMD ["python", "handler.py"]
