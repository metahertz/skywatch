FROM python:3.12-slim-bookworm
# docker-ce-cli + docker-compose-plugin live in Docker's own apt repo,
# not Debian's; install per https://docs.docker.com/engine/install/debian/.
# We only need the CLI — the daemon runs on the host and is reached via
# the bind-mounted /var/run/docker.sock.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
         -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
         > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt
COPY app.py ./
COPY templates ./templates
COPY static ./static
EXPOSE 8090
CMD ["python3", "app.py"]
