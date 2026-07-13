FROM python:3.14

WORKDIR /app

COPY tgw-1.0.8.7-py3-none-any.whl .
COPY AmazingData-1.1.7-cp314-none-any.whl .

RUN pip install --no-cache-dir \
    ./tgw-1.0.8.7-py3-none-any.whl

RUN pip install --no-cache-dir \
    ./AmazingData-1.1.7-cp314-none-any.whl

COPY pyproject.toml .
COPY app/ app/
COPY scripts/ scripts/

RUN pip install --no-cache-dir .

EXPOSE 3021
CMD ["uvicorn", "app.http_app:app", "--host", "0.0.0.0", "--port", "3021"]
