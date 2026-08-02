FROM python:3.13.6-alpine

ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apk update \
	&& apk add --no-cache gettext gcc musl-dev postgresql-dev python3-dev libffi-dev \
	&& pip install --upgrade pip

COPY ./requirements.txt ./

RUN pip install -r requirements.txt

COPY ./ ./

# Crear directorio para la base de datos con permisos
RUN mkdir -p /app/data && chmod 777 /app/data

CMD ["sh", "entrypoint.sh"]
