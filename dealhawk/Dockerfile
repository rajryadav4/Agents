FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY dealhawk ./dealhawk
RUN pip install --no-cache-dir ".[claude]"
# Optional, for `browser: true` retailer sources (adds ~400 MB):
#   RUN pip install playwright && playwright install --with-deps chromium
VOLUME /data
WORKDIR /data
EXPOSE 8787
ENV DEALHAWK_DB=/data/dealhawk.db
ENTRYPOINT ["dealhawk"]
CMD ["run", "-c", "/data/dealhawk.yaml"]
