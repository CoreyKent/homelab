FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

RUN groupadd --gid 1000 collector \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin collector

COPY collector/ /app/collector

WORKDIR /app
USER collector

ENTRYPOINT ["python","-m","collector.main"]
