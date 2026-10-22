# syntax=docker/dockerfile:1
FROM node:24.18.0-alpine AS build

WORKDIR /app

RUN npm install --global pnpm@11.22.0

COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY frontend/ ./
RUN pnpm build

FROM nginx:1.29-alpine

COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
