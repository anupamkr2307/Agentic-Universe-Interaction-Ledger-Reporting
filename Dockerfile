FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code and data
COPY . .

# Run tests and seed initial reports
RUN pytest && \
    python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data && \
    python -m solution.cli ingest data/drop_1 && \
    python -m solution.cli ingest data/drop_2 && \
    python -m solution.cli report --out reports/

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn solution.app:app --host 0.0.0.0 --port ${PORT}"]
