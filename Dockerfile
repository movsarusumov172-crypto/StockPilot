FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV STOCKPILOT_DATABASE_URL=sqlite:////app/data/stockpilot.db
VOLUME ["/app/data"]

ENTRYPOINT ["stock"]
