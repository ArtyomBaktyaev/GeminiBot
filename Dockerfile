FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/opt/study-browsers
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps --only-shell chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends fonts-texgyre-math fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home study && chown study:study /app
COPY --chown=study:study *.py ./
COPY --chown=study:study fonts/ fonts/
COPY --chown=study:study webapp/ webapp/
COPY --chown=study:study study_print/ study_print/
USER study
CMD ["python", "bot.py"]
