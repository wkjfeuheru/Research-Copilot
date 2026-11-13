FROM node:24-bookworm-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c AS node
FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MPLCONFIGDIR=/tmp/matplotlib
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk=1:20220127+repack1-1 \
    && rm -rf /var/lib/apt/lists/*
COPY docker/analysist-sandbox/requirements.lock /opt/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /opt/requirements.lock
WORKDIR /opt/renderer
COPY docker/analysist-sandbox/package.json docker/analysist-sandbox/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts && npm cache clean --force
COPY docker/analysist-sandbox/render_echarts.mjs ./
COPY src/research_copilot/resources/skills/analysist/financial-modeling/scripts/chart_policy.* ./
COPY src/research_copilot/resources/skills/analysist/financial-modeling/scripts/chart_theme.json ./
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin researcher \
    && mkdir -p /outputs /workspace /skills && chown 10001:10001 /outputs
USER 10001:10001
WORKDIR /outputs
CMD ["sleep", "infinity"]
