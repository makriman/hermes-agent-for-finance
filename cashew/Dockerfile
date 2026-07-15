FROM python:3.12-slim

WORKDIR /app

# deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# code (data + clock state are mounted at runtime, not baked in)
COPY mocks ./mocks
COPY sim ./sim

ENV CASHEW_DATA_ROOT=/app/data/ailabgemini \
    CASHEW_STATE_FILE=/app/state/.sim_state.json \
    PYTHONUNBUFFERED=1

EXPOSE 8900

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8900/health',timeout=3).status==200 else 1)"

CMD ["uvicorn", "mocks.app:app", "--host", "0.0.0.0", "--port", "8900"]
