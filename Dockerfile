FROM python:3.12-bookworm AS locale-builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends locales \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/locale \
    && localedef --no-archive \
        --inputfile=zh_CN \
        --charmap=GB18030 \
        /opt/locale/zh_CN.GB18030

FROM python:3.12-slim-bookworm

ARG UV_VERSION=0.11.32

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN curl --retry 5 --retry-all-errors --fail --location --silent --show-error \
        "https://astral.sh/uv/${UV_VERSION}/install.sh" \
        --output /tmp/uv-installer.sh \
    && UV_UNMANAGED_INSTALL=/usr/local/bin sh /tmp/uv-installer.sh \
    && rm /tmp/uv-installer.sh

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

COPY --from=locale-builder /opt/locale/zh_CN.GB18030 /usr/lib/locale/zh_CN.GB18030

ENV LANG=C \
    LC_ALL=C

COPY src ./src
RUN mkdir -p flow/md flow/td logs

CMD ["uv", "run", "--no-sync", "python", "src/main.py"]
