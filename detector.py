"""Ловушка ID: запусти и напиши любое сообщение в нужную тему группы."""
import asyncio
import os

from aiogram import Bot, Dispatcher, types

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not BOT_TOKEN:
    raise SystemExit("Задай TELEGRAM_BOT_TOKEN в переменной окружения")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message()
async def catch_ids(message: types.Message):
    print("\n" + "=" * 40)
    print(f"Группа: {message.chat.title}")
    print(f"CHAT_ID: {message.chat.id}")
    print(f"ID темы (message_thread_id): {message.message_thread_id}")
    print(f"Текст сообщения: {message.text}")
    print("=" * 40 + "\n")


async def main():
    print("Ловушка запущена! Напиши сообщение в нужную тему группы...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
