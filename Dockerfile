FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FILEMATCH_HOST=0.0.0.0

RUN useradd --create-home --uid 10001 filematch
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install .

USER filematch
EXPOSE 8765
CMD ["filematch"]
