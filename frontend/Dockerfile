FROM nginxinc/nginx-unprivileged:1.30-alpine
# The base image ships brotli-libs 1.2.0-r1 (CVE-2026-111497); Alpine 3.24 has
# the fix (1.2.1-r0). Drop this once the base image is rebuilt with it.
USER root
RUN apk upgrade --no-cache brotli-libs
USER 101
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html /usr/share/nginx/html/index.html
