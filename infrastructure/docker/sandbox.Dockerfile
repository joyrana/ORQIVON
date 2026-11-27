# Code sandbox image for SANDBOX_BACKEND=docker: the same toolchain `make sandbox-toolchain` installs
# for the namespace sandbox. Generated tests run as `/opt/asw-sandbox/bin/python -m pytest` with the
# evidence plugin copied into the workspace, so the image needs only Python and pytest. The toolchain
# is root-owned and read-only for everyone else; the container also runs with a read-only root, no
# network and no capabilities (see execution/sandbox.py).
#
#   docker build -f infrastructure/docker/sandbox.Dockerfile -t orqivon-sandbox:1 .
FROM python:3.12-slim

RUN python -m venv /opt/asw-sandbox \
 && /opt/asw-sandbox/bin/pip install --no-cache-dir --disable-pip-version-check "pytest==9.2.0" \
 && chown -R root:root /opt/asw-sandbox \
 && chmod -R u=rwX,go=rX /opt/asw-sandbox \
 && useradd --system --no-create-home --shell /usr/sbin/nologin --uid 64000 sandbox

USER 64000
WORKDIR /workspace
