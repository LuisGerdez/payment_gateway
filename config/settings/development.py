from .base import *

SECRET_KEY = 'development-secret-key'

DEBUG = True

ALLOWED_HOSTS = ['localhost', '127.0.0.1', '10.0.2.2', '*']

CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_HEADERS = ["Authorization", "Content-Type", "Accept"]
CORS_ALLOW_CREDENTIALS = True

# Database
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}