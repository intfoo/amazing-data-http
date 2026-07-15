FROM python:3.14

WORKDIR /app

COPY tgw-*-py3-none-any.whl ./
COPY AmazingData-*-cp314-none-any.whl ./

RUN pip install --no-cache-dir ./tgw-*-py3-none-any.whl

RUN pip install --no-cache-dir ./AmazingData-*-cp314-none-any.whl

COPY pyproject.toml .
COPY app/ app/
COPY scripts/ scripts/

RUN pip install --no-cache-dir .

EXPOSE 3021
CMD ["uvicorn", "app.http_app:app", "--host", "0.0.0.0", "--port", "3021"]
