FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dispatch/ ./dispatch/

EXPOSE 8092

CMD ["uvicorn", "dispatch.server:app", "--host", "0.0.0.0", "--port", "8092"]
