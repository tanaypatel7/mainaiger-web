FROM python:3.13.18-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MAINAIGER_CONTAINER=1
WORKDIR /app
RUN mkdir -p /state /workspace && chown 10001:10001 /state /workspace && chmod 0700 /state
COPY companion /app/companion
USER 10001:10001
CMD ["python", "-m", "companion", "--state-dir", "/state", "--workspace", "/workspace", "--container-listen"]
