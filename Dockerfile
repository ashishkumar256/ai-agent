FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/agent

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --no-compile -r requirements.txt \
    && groupadd --system agent \
    && useradd --system --gid agent --home-dir /home/agent agent \
    && mkdir -p /home/agent/manifest \
    && chown -R agent:agent /home/agent

COPY --chown=agent:agent k8s_agent.py ./
USER agent

CMD ["python", "-u", "k8s_agent.py"]
