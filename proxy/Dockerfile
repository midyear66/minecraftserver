FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mc_proxy.py notifications.py ./

CMD ["python", "-u", "mc_proxy.py"]
