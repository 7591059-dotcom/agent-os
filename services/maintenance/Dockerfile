FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi==0.141.1 uvicorn==0.53.0 jsonschema==4.26.0 && useradd --uid 10002 --no-create-home worker && mkdir /proposals && chown 10002:10002 /proposals
WORKDIR /service
COPY services/maintenance/app.py /service/app.py
USER 10002:10002
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8010","--no-access-log","--no-proxy-headers"]
