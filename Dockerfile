FROM python:3.12-slim

WORKDIR /app

# build-essential is needed to compile the `netifaces` extension (a
# tinytuya dependency, used for local Tuya device discovery) - there's no
# prebuilt wheel for it on Python 3.12.
RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
