FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 GRUMP_DATA=/data/grump
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 && rm -rf /var/lib/apt/lists/*
RUN useradd --uid 10001 --create-home mu && mkdir -p /data/grump && chown mu:mu /data/grump
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mu_calculator ./mu_calculator
USER mu
VOLUME ["/data/grump"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=180m CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"
ENTRYPOINT ["python", "-m", "mu_calculator"]
CMD ["serve"]
