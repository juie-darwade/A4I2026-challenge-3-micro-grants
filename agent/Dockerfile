FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY grant_advisor/ /app/agents/grant_advisor/
RUN useradd -m appuser && chown -R appuser /app
USER appuser
ENV PORT=8080
# adk web serves the chat UI and the REST API (/run, /run_sse) the front-end lane can call.
CMD ["sh", "-c", "adk web --host 0.0.0.0 --port ${PORT} /app/agents"]
