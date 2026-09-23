FROM python:3.12-slim
LABEL org.opencontainers.image.title="ABS Duplicate Finder" \
      org.opencontainers.image.description="Find, compare and clean up duplicate items in Audiobookshelf" \
      org.opencontainers.image.licenses="MIT"
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY abs_dupes.pyw .
ENV IN_DOCKER=1 PORT=5057 PYTHONUNBUFFERED=1
EXPOSE 5057
VOLUME /config
CMD ["python", "/app/abs_dupes.pyw"]
