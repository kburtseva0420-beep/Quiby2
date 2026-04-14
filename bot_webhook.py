import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, InlineQueryHandler, ChosenInlineResultHandler
from aiohttp import web
import asyncio

# Импортируем всё из основного бота
from bot import (
    quiz_game, start, quiz, stats, 
    top, chatstats, quizreset,
    help_command, answer, next_question_handler, inline_query, chosen_inline_result
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO
)

# Получаем токен и webhook URL из переменных окружения
TOKEN = os.environ.get("BOT_TOKEN", "8196459604:AAFI5Vps5l9yN3mGFoNKpGcwo9JQqxjNDVg")
WEBHOOK_URL = os.environ.get("RAILWAY_PUBLIC_DOMAIN")  # Railway автоматически устанавливает
PORT = int(os.environ.get("PORT", 8080))

# Telegram Application
application = Application.builder().token(TOKEN).build()

# Регистрируем обработчики
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("help", help_command))
application.add_handler(CommandHandler("quiz", quiz))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("top", top))
application.add_handler(CommandHandler("chatstats", chatstats))
application.add_handler(CommandHandler("quizreset", quizreset))
application.add_handler(CallbackQueryHandler(answer, pattern=r"^answer:"))
application.add_handler(CallbackQueryHandler(next_question_handler, pattern=r"^next_question$"))
application.add_handler(InlineQueryHandler(inline_query))
application.add_handler(ChosenInlineResultHandler(chosen_inline_result))

async def index(request):
    return web.Response(text="Quiz Bot is running!")

async def webhook_handler(request):
    """Обработка webhook от Telegram"""
    json_data = await request.json()
    update = Update.de_json(json_data, application.bot)
    await application.process_update(update)
    return web.Response(text="OK")

async def setup_webhook():
    """Установка webhook"""
    if WEBHOOK_URL:
        webhook_url = f"https://{WEBHOOK_URL}/{TOKEN}"
        await application.bot.set_webhook(url=webhook_url)
        logging.info(f"Webhook установлен: {webhook_url}")
    else:
        logging.warning("RAILWAY_PUBLIC_DOMAIN не установлен, webhook не настроен!")

async def main():
    # Инициализируем приложение
    await application.initialize()
    await application.start()
    await setup_webhook()
    
    # Создаём веб-сервер
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post(f"/{TOKEN}", webhook_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    
    logging.info(f"Сервер запущен на порту {PORT}")
    
    # Держим сервер запущенным
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())