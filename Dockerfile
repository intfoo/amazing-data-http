FROM python:3.14-slim

# 设置时区为 Asia/Shanghai（UTC+8），与宿主机一致。
# 必须装 tzdata + 软链 /etc/localtime，仅设 ENV TZ 不够：
# datetime.now() 和日志时间戳读 /etc/localtime，python:3.14-slim 默认无 tzdata。
# 影响点：kline_service.py 的 datetime.now()（/minute 默认区间）、日志 asctime。
# libgomp1：numpy 运行时需要 libgomp.so.1（OpenMP），slim 镜像默认无（full 镜像随 gcc 附带）。
#   缺失会导致 import numpy 报 ImportError: libgomp.so.1: cannot open shared object file。
ENV TZ=Asia/Shanghai
RUN apt-get update && apt-get install -y --no-install-recommends tzdata libgomp1 && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone && \
    rm -rf /var/lib/apt/lists/*

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
# 注意：此处 host/port 写死，未读取 .env 的 HTTP_HOST/HTTP_PORT。
# 如需支持自定义端口，改为：CMD ["sh", "-c", "uvicorn app.http_app:app --host $HTTP_HOST --port $HTTP_PORT"]
# 并同步修改 docker-compose.yml 的 ports 映射右侧。
CMD ["uvicorn", "app.http_app:app", "--host", "0.0.0.0", "--port", "3021"]
