# Use Python 3.13 slim image for smaller size
FROM python:3.13-slim

# Set working directory
WORKDIR /app

# Copy application code first (needed for editable install)
COPY . .

# Install Python dependencies
RUN pip install --no-cache-dir -e .

# Expose the HTTPS port
EXPOSE 443

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Generate self-signed adhoc certificate for the container (valid for 365 days)
RUN openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout /app/key.pem -out /app/cert.pem \
    -days 365 -subj "/CN=localhost"

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5080/', timeout=5)" || exit 1

# Run with gunicorn
CMD ["gunicorn", "-c", "gunicorn.conf.py", "web_app:app"]
