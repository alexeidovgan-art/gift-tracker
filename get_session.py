"""
Разовый скрипт. У бесплатного Render нет консоли с вводом текста,
поэтому запусти это в Google Colab (colab.research.google.com,
бесплатно, без карты): новая записная книжка -> в первой ячейке
    !pip install telethon -q
-> запустить -> во второй ячейке вставить содержимое этого файла
-> запустить и вводить данные в появляющиеся поля.

Он выведет длинную строку — это и есть TG_SESSION.
Скопируй её и вставь в Render -> Environment -> переменная TG_SESSION.
После этого get_session.py (и записную книжку в Colab) можно удалить.
"""
from telethon import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("API_ID: ").strip())
api_hash = input("API_HASH: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\n✅ Готово. Вот твоя TG_SESSION (скопируй целиком):\n")
    print(client.session.save())
    print("\nВставь её в Render -> Environment -> TG_SESSION.")
