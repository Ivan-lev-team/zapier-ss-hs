FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5055

CMD ["gunicorn", "--workers", "2", "--bind", "0.0.0.0:5055", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
