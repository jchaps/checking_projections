FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

ENTRYPOINT ["python", "-m", "app.main"]
CMD ["run"]
