FROM python:3.11-alpine
RUN apk add --no-cache bash git curl ripgrep coreutils
WORKDIR /opt/dev-agent
COPY requirements.txt /opt/dev-agent/requirements.txt
RUN pip install --no-cache-dir -r /opt/dev-agent/requirements.txt
COPY agent.py SYSTEM_PROMPT.md /opt/dev-agent/
RUN chmod +x /opt/dev-agent/agent.py
WORKDIR /workspace
ENV AGENT_WORKSPACE=/workspace
ENTRYPOINT ["python3", "/opt/dev-agent/agent.py"]
