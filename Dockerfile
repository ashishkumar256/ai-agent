# Extract git commit info
FROM ashishkumar256/gitinfo AS gitinfo
COPY .git .git
RUN create-git-env.sh

# Final app image
FROM python:3.12-slim
COPY --from=gitinfo /src/.env.git .env.git

# Optional: turn the values into Docker labels
RUN eval "$(cat .env.git)"

ARG COMMIT_ID COMMIT_MSG COMMIT_EMAIL COMMIT_NAME COMMIT_URL

LABEL git.commit_id="${COMMIT_ID}" \
      git.commit_msg="${COMMIT_MSG}" \
      git.commit_email="${COMMIT_EMAIL}" \
      git.commit_name="${COMMIT_NAME}" \
      git.commit_url="${COMMIT_URL}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/agent

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --no-compile -r requirements.txt \
    && groupadd --system agent \
    && useradd --system --gid agent --create-home --home-dir /home/agent agent \
    && chown -R agent:agent /home/agent

COPY --chown=agent:agent k8s_agent.py ./
USER agent

CMD ["python", "-u", "k8s_agent.py"]
