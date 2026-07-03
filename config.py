import os
from dotenv import load_dotenv

# Загружаем переменные окружения из файла .env, лежащего рядом с проектом
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DGIS_API_KEY = os.getenv("DGIS_API_KEY")

if not TELEGRAM_TOKEN:
    raise RuntimeError(
        "Не задан TELEGRAM_TOKEN. Создай файл .env на основе .env.example "
        "и укажи в нём токен, полученный у @BotFather."
    )

if not DGIS_API_KEY:
    raise RuntimeError(
        "Не задан DGIS_API_KEY. Создай файл .env на основе .env.example "
        "и укажи в нём ключ 2ГИС API (https://dev.2gis.com/)."
    )
