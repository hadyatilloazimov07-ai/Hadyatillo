"""aiogram router: business updates go to the Assistant, owner commands in the bot's DM."""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BusinessConnection, Message

from .service import Assistant

ALLOWED_UPDATES = ["message", "business_connection", "business_message"]

STRANGER_START_TEXT = (
    "Salom! Bu shaxsiy yordamchi bot, u faqat egasi uchun ishlaydi. "
    "Bu yerda suhbatlashib bo'lmaydi — iltimos, egasiga to'g'ridan-to'g'ri yozing."
)


def build_router(assistant: Assistant) -> Router:
    router = Router(name="assistant")
    owner_id = assistant.settings.owner_id

    @router.business_connection()
    async def on_connection(connection: BusinessConnection) -> None:
        await assistant.on_business_connection(connection)

    @router.business_message()
    async def on_business_message(message: Message, bot: Bot) -> None:
        await assistant.on_business_message(bot, message)

    private = F.chat.type == "private"
    is_owner = F.from_user.id == owner_id

    @router.message(CommandStart(), private)
    async def on_start(message: Message, bot: Bot) -> None:
        if message.from_user is not None and message.from_user.id == owner_id:
            me = await bot.me()
            await message.answer(assistant.start_text(me.username))
        else:
            await message.answer(STRANGER_START_TEXT)

    @router.message(Command("pause"), private, is_owner)
    async def on_pause(message: Message) -> None:
        assistant.pause()
        await message.answer("Paused. Business chats will not get replies until /resume.")

    @router.message(Command("resume"), private, is_owner)
    async def on_resume(message: Message) -> None:
        assistant.resume()
        await message.answer("Resumed.\n\n" + assistant.status_text())

    @router.message(Command("status"), private, is_owner)
    async def on_status(message: Message) -> None:
        await message.answer(assistant.status_text())

    @router.message()
    async def ignore_everything_else(message: Message) -> None:
        """Other direct messages (strangers included) are silently ignored."""

    return router
