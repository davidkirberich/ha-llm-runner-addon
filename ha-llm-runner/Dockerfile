ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.20
FROM ${BUILD_FROM}

WORKDIR /app

# Build dependencies for numpy, pandas, pydantic
RUN apk add --no-cache \
    gcc \
    g++ \
    musl-dev \
    linux-headers \
    libffi-dev

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY run.py .
RUN chmod a+x run.py

CMD [ "python3", "/app/run.py" ]