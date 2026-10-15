FROM python:3.12-slim

ARG AGENT_UID=1000
ARG AGENT_GID=1000

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/agent \
    MANIFEST_DIR=/manifest

# Non-root user; UID/GID can match the host so files in ./manifest are yours
RUN groupadd --gid ${AGENT_GID} agent \
 && useradd --uid ${AGENT_UID} --gid ${AGENT_GID} --create-home --home-dir /home/agent agent \
 && mkdir -p /manifest /home/agent/.kube \
 && chown -R agent:agent /manifest /home/agent

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY k8s_agent.py .

USER agent
VOLUME ["/manifest"]

# Interactive tool: run with a TTY (docker compose run --rm k8s-agent)
CMD ["python", "k8s_agent.py"]
