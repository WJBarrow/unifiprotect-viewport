FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir requests
COPY service.py .
EXPOSE 8686
CMD ["python", "-u", "service.py"]
