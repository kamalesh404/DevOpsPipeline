FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

COPY requirements.txt pyproject.toml setup.py README.md ./
COPY src ./src
COPY cli ./cli

RUN python -m pip install --upgrade pip \
    && python -m pip wheel --wheel-dir /wheels -r requirements.txt \
    && python -m pip wheel --wheel-dir /wheels .

FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="DevOpsPipeline" \
      org.opencontainers.image.description="Extensible Python CI/CD platform" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/home/appuser/.local/bin:${PATH}"

RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --user /wheels/*.whl \
    && rm -rf /wheels

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health',timeout=3)"

CMD ["python", "-m", "uvicorn", "src.dashboard.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
