#!/bin/sh

echo 'Running collecstatic...'
python manage.py collectstatic --no-input --settings=config.settings.testing

echo 'Applying migrations...'
python manage.py migrate --settings=config.settings.testing

echo 'Running server...'
#python manage.py runserver --settings=config.settings.development 0.0.0.0:8000 # For development
gunicorn --env DJANGO_SETTINGS_MODULE=config.settings.testing config.wsgi:application --bind 0.0.0.0:8000 # For production with WSGI
#gunicorn --env DJANGO_SETTINGS_MODULE=config.settings.testing config.asgi:application --worker-class uvicorn.workers.UvicornWorker --workers 4 --bind 0.0.0.0:8000 # For production with ASGI