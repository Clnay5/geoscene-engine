FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY geoscene_engine ./geoscene_engine
RUN pip install --no-cache-dir .
EXPOSE 8091
CMD ["python", "-m", "uvicorn", "geoscene_engine.api:app", "--host", "0.0.0.0", "--port", "8091", "--workers", "1"]
