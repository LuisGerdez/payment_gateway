from .base import *

SECRET_KEY = "testing-secret-key"

DEBUG = True

ALLOWED_HOSTS = ['localhost', '127.0.0.1', '10.0.2.2', '*']

# CORS Settings
CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_HEADERS = ["Authorization", "Content-Type", "Accept"]
CORS_ALLOW_CREDENTIALS = True

# Database
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'payment_gateway_db',
        'USER': 'fc_admin',
        'PASSWORD': 'kck2026',
        'HOST': 'localhost',
        'PORT': '5432',
    }
}

