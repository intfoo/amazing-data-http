FROM python:3.14-slim

# 设置时区为 Asia/Shanghai（UTC+8），与宿主机一致。
# 必须装 tzdata + 软链 /etc/localtime，仅设 ENV TZ 不够：
# datetime.now() 和日志时间戳读 /etc/localtime，python:3.14-slim 默认无 tzdata。
# 影响点：kline_service.py 的 datetime.now()（/minute 默认区间）、日志 asctime。
# libgomp1：numpy 运行时需要 libgomp.so.1（OpenMP），slim 镜像默认无（full 镜像随 gcc 附带）。
#   缺失会导致 import numpy 报 ImportError: libgomp.so.1: cannot open shared object file。
# libkrb5-3 + libgssapi-krb5-2：tgw 自带的 libssl.so.10（OpenSSL 1.0）依赖 Kerberos 库
#   （libgssapi_krb5/libkrb5/libcom_err/libk5crypto）。slim 镜像默认无，缺失会导致
#   import tgw 报 "No module named 'libtgw_python314'"（dlopen 失败被 SWIG 吞掉）。
#   libgssapi-krb5-2 不是 libkrb5-3 的直接依赖，须显式声明。
ENV TZ=Asia/Shanghai
# tgw SDK 的 libtgw_python314.so 依赖 common_linux_lib64/ 下的 .so（libtgw.so/libcrypto.so.10/libpcap.so.1 等），
# 需通过 LD_LIBRARY_PATH 让 dlopen 找到它们。python:3.14 full 镜像恰好有系统库满足，slim 没有。
# 缺失时报 "No module named 'libtgw_python314'"（SWIG import 吞掉了真实的 dlopen 错误）。
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.14/site-packages/tgw/common_linux_lib64:${LD_LIBRARY_PATH:-}
# 设置国内镜像源
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources && \
    sed -i 's|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources
RUN apt-get update && apt-get install -y --no-install-recommends tzdata libgomp1 libkrb5-3 libgssapi-krb5-2 && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone && \
    rm -rf /var/lib/apt/lists/*

# pip 使用清华镜像源加速依赖下载（tgw/AmazingData 本地 wheel 不受影响，仅影响其依赖如 pandas/numpy）
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple/
ENV PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

WORKDIR /app

COPY tgw-*-py3-none-any.whl ./
COPY AmazingData-*-cp314-none-any.whl ./

RUN pip install --no-cache-dir ./tgw-*-py3-none-any.whl

RUN pip install --no-cache-dir ./AmazingData-*-cp314-none-any.whl

COPY pyproject.toml .
COPY app/ app/
COPY scripts/ scripts/

RUN pip install --no-cache-dir .

# 诊断：列出 libtgw_python314.so 的依赖链，"not found" 即为 slim 缺失的系统库
RUN ldd /usr/local/lib/python3.14/site-packages/tgw/linux_py314_x64_package/libtgw_python314.so 2>&1 || true

EXPOSE 3021
# 注意：此处 host/port 写死，未读取 .env 的 HTTP_HOST/HTTP_PORT。
# 如需支持自定义端口，改为：CMD ["sh", "-c", "uvicorn app.http_app:app --host $HTTP_HOST --port $HTTP_PORT"]
# 并同步修改 docker-compose.yml 的 ports 映射右侧。
CMD ["uvicorn", "app.http_app:app", "--host", "0.0.0.0", "--port", "3021"]
