FROM python:3.12-bookworm AS locale-builder

ARG DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn

RUN sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 -o Acquire::https::Timeout=60 update \
    && apt-get install -y --no-install-recommends locales \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/locale \
    && localedef --no-archive \
        --inputfile=zh_CN \
        --charmap=GB18030 \
        /opt/locale/zh_CN.GB18030

FROM python:3.12-slim-bookworm

ARG UV_VERSION=0.11.32
ARG DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_HTTP_RETRIES=5 \
    UV_HTTP_TIMEOUT=120 \
    UV_CONCURRENT_DOWNLOADS=4

RUN sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 -o Acquire::https::Timeout=60 update \
    && apt-get install -y --no-install-recommends ca-certificates curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN curl --retry 5 --retry-all-errors --fail --location --silent --show-error \
        "https://astral.sh/uv/${UV_VERSION}/install.sh" \
        --output /tmp/uv-installer.sh \
    && UV_UNMANAGED_INSTALL=/usr/local/bin sh /tmp/uv-installer.sh \
    && rm /tmp/uv-installer.sh

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

COPY --from=locale-builder /opt/locale/zh_CN.GB18030 /usr/lib/locale/zh_CN.GB18030

ENV LANG=C \
    LC_ALL=C

COPY src ./src
RUN mkdir -p flow/md flow/td logs

CMD ["uv", "run", "--no-sync", "python", "src/main.py", "--connect-timeout", "300"]
