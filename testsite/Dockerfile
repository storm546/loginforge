FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir "flask>=3.0"
WORKDIR /app
COPY app.py mock_2captcha.py entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh
EXPOSE 5000 8081
ENTRYPOINT ["/app/entrypoint.sh"]
