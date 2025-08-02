import logging
import json
import os
import re
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import gspread
from google.oauth2.service_account import Credentials
from openai import OpenAI
from config import TELEGRAM_TOKEN, GOOGLE_SHEET_ID, SHEET_NAME, OPENAI_API_KEY
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
import threading
import calendar

# Московское время
MOSCOW_TZ = pytz.timezone('Europe/Moscow')

def get_moscow_time():
    """Возвращает текущее московское время"""
    return datetime.now(MOSCOW_TZ)

def format_moscow_date():
    """Возвращает дату в московском времени в формате ДД.ММ.ГГГГ"""
    return get_moscow_time().strftime('%d.%m.%Y')

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Настройка OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

import json
import os

# Читаем credentials из переменной окружения
creds_json = os.getenv('GOOGLE_CREDENTIALS')
if creds_json:
    creds_info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
else:
    # Fallback на файл для локальной разработки
    creds = Credentials.from_service_account_file('credentials.json', scopes=SCOPES)

# Открываем таблицы
finance_sheet = gc.open_by_key(GOOGLE_SHEET_ID).worksheet(SHEET_NAME)

# Хранилище последних операций и контекста
USER_LAST_OPERATIONS = {}
USER_CONTEXT = {}

# Добавляем функцию проверки username
ALLOWED_USERNAME = 'antigorevich'

def is_allowed_user(update: Update):
    user = update.effective_user
    return user and user.username and user.username.lower() == ALLOWED_USERNAME

def analyze_message_with_ai(text, user_context=None):
    """Анализирует сообщение с помощью ИИ с учетом контекста"""

    # Сначала проверяем, не является ли это командным запросом
    command_result = parse_voice_command(text)
    if command_result:
        return command_result

    context_info = ""
    if user_context:
        recent_operations = user_context.get('recent_operations', [])
        if recent_operations:
            context_info = f"""
КОНТЕКСТ последних операций пользователя:
{chr(10).join(recent_operations[-5:])}

Используй этот контекст для более точного понимания. Например:
- Если говорит "такая же сумма" - ищи в контексте
- Если "тому же человеку" - используй имя из предыдущих операций
- Если просто "зарплата" без имени - предложи уточнить или используй контекст
"""

    prompt = f"""
Проанализируй сообщение пользователя и определи его тип и данные.

{context_info}

Сообщение: "{text}"

Верни JSON в следующем формате:

Для ФИНАНСОВЫХ операций:
{{
    "type": "finance",
    "operation_type": "Пополнение" или "Расход",
    "amount": число (положительное для пополнения, отрицательное для расхода),
    "category": одна из категорий: "Зарплаты сотрудникам", "Выплаты учредителям", "Оплата поставщику", "Процент", "Закупка товара", "Материалы", "Транспорт", "Связь", "Такси", "Общественные расходы", "Благотворительность", "-" (для пополнений),
    "description": "краткое описание с именами людей",
    "comment": "",
    "confidence": число от 0 до 1 (насколько уверен в распознавании)
}}

Если НЕЯСНО или нужно УТОЧНЕНИЕ:
{{
    "type": "clarification",
    "message": "Уточняющий вопрос пользователю",
    "suggestions": ["вариант 1", "вариант 2", "вариант 3"]
}}

ПРАВИЛА РАСПОЗНАВАНИЯ:

1. ФИНАНСЫ - точные индикаторы:
   - Пополнения: "пополнил", "снял", "взял наличку", "получил деньги" = Пополнение
   - Расходы: "заплатил", "потратил", "дал", "купил", "оплатил", "зарплата" = Расход

2. КАТЕГОРИИ - строгие правила:
   - "дал/заплатил/зарплата + ИМЯ" = "Зарплаты сотрудникам"
   - "Таня лично/Игорь лично/Антон лично" = "Выплаты учредителям"
   - "материалы/закупка/товары" = "Материалы"
   - "такси/убер/яндекс" = "Такси"
   - "транспорт/бензин/авто/Герасимов" = "Транспорт"
   - "связь/интернет/телефон" = "Связь"
   - "благотворительность/донат/помощь/СВО" = "Благотворительность"
   - "хоз расходы/хозяйственные/офис/канцелярия" = "Общественные расходы"

3. ОПИСАНИЕ - только суть, с заглавной буквы:
   - Убирай: "заплатил", "дал", "потратил", "купил", "оплатил", "лично"
   - Оставляй: имена, должности, назначение

4. ВСЕГДА ВЫСОКАЯ УВЕРЕННОСТЬ:
   - Если в сообщении есть ЧИСЛО - confidence = 0.9
   - НЕ задавай уточняющих вопросов если есть сумма
   - Лучше записать что-то чем спрашивать

5. ОБРАБОТКА ПАДЕЖНЫХ ОКОНЧАНИЙ:
   - "Балтики" → "Балтика", "Рустаму" → "Рустам", "Петрову" → "Петров"
   - "Интигаму" → "Интигам", "Сидорову" → "Сидоров"

ВАЖНО: НИКОГДА НЕ УТОЧНЯЙ НИЧЕГО ЕСЛИ В СООБЩЕНИИ ЕСТЬ ЧИСЛО!

6. КОНТЕКСТНЫЕ ФРАЗЫ:
   - "такая же сумма" = ищи последнюю сумму в контексте
   - "тому же" = используй последнего получателя
   - "как вчера" = анализируй контекст за вчера
   - "обычная зарплата Петрову" = если есть в контексте - используй, иначе уточни

ВАЖНО: Если confidence < 0.7 или данных недостаточно - лучше уточнить чем ошибиться!
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты эксперт по анализу финансовых операций. Точность критически важна. При сомнениях - всегда уточняй."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1
        )

        result = response.choices[0].message.content.strip()
        # Убираем markdown форматирование если есть
        if result.startswith("```json"):
            result = result[7:-3]
        elif result.startswith("```"):
            result = result[3:-3]

        return json.loads(result)

    except Exception as e:
        logger.error(f"Ошибка ИИ анализа: {e}")
        return {"type": "clarification", "message": "Извините, произошла ошибка. Попробуйте переформулировать.", "suggestions": []}

def update_user_context(user_id, operation_data):
    """Обновляет контекст пользователя"""
    if user_id not in USER_CONTEXT:
        USER_CONTEXT[user_id] = {'recent_operations': []}

    # Формируем строку операции для контекста
    context_line = f"{operation_data['data']['description']}: {operation_data['data']['amount']:,.0f} ₽ ({operation_data['data']['category']})"

    USER_CONTEXT[user_id]['recent_operations'].append(context_line)

    # Храним только последние 10 операций
    if len(USER_CONTEXT[user_id]['recent_operations']) > 10:
        USER_CONTEXT[user_id]['recent_operations'] = USER_CONTEXT[user_id]['recent_operations'][-10:]

def add_finance_record(data, user_id):
    """Добавляет финансовую запись в таблицу"""
    try:
        row = [
            format_moscow_date(),  # Московское время
            data['operation_type'],
            data['category'],
            data['description'],
            data['amount'],
            data.get('comment', '')
        ]
        finance_sheet.append_row(row)

        # Сохраняем последнюю операцию
        USER_LAST_OPERATIONS[user_id] = {
            'type': 'finance',
            'data': data,
            'row': len(finance_sheet.get_all_values()),
            'timestamp': get_moscow_time()
        }

        # Обновляем контекст
        update_user_context(user_id, USER_LAST_OPERATIONS[user_id])

        return True
    except Exception as e:
        logger.error(f"Ошибка записи финансов: {e}")
        return False

def parse_voice_command(text):
    """Парсит голосовые команды и возвращает соответствующую команду"""
    text_lower = text.lower()

    # Команды по получателям (НОВОЕ!)
    if any(phrase in text_lower for phrase in ['кому платили', 'анализ получателей', 'по получателям', 'кому больше', 'топ получателей']):
        return {"type": "voice_command", "command": "recipients", "params": text}

    # Команды по поставщикам (ПРИОРИТЕТ!)
    if any(phrase in text_lower for phrase in ['анализ поставщика', 'по поставщику', 'история с', 'поставщик']):
        return {"type": "voice_command", "command": "suppliers", "params": text}

    # Команды аналитики
    if any(phrase in text_lower for phrase in ['анализ', 'аналитика', 'отчет', 'покажи траты', 'сколько потратили']):
        return {"type": "voice_command", "command": "analytics", "params": text}

    # Команды поиска
    if any(phrase in text_lower for phrase in ['найди', 'найти', 'поиск', 'покажи операции', 'когда платили']):
        return {"type": "voice_command", "command": "search", "params": text}

    # Команды по категориям
    if any(phrase in text_lower for phrase in ['по категориям', 'категории', 'расходы по']):
        return {"type": "voice_command", "command": "categories", "params": text}

    # Команды истории
    if any(phrase in text_lower for phrase in ['история', 'последние операции', 'что было']):
        return {"type": "voice_command", "command": "history", "params": text}

    # Команды бэкапа
    if any(phrase in text_lower for phrase in ['бэкап', 'резервная копия', 'сохрани', 'backup']):
        return {"type": "voice_command", "command": "backup", "params": text}

    return None

def extract_params_from_voice(text, command_type):
    """Извлекает параметры из голосового запроса"""
    text_lower = text.lower()
    params = {}

    # Извлекаем имена/компании для команд поставщиков
    if command_type == 'suppliers':
        # Ищем после ключевых слов "поставщика", "поставщику", "с"
        patterns = [
            r'поставщика\s+([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?)',
            r'поставщику\s+([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?)',
            r'история\s+с\s+([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?)',
            r'по\s+([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?)',
            r'анализ\s+([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?)'
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                name = match.group(1).strip()
                # Приводим к стандартному виду
                if name.lower() in ['интигаму', 'интигама']:
                    params['name'] = 'Интигам'
                elif name.lower() in ['балтики', 'балтике', 'балтику']:
                    params['name'] = 'Балтика'
                elif name.lower() in ['петрову', 'петрова']:
                    params['name'] = 'Петров'
                elif name.lower() in ['рустаму', 'рустама']:
                    params['name'] = 'Рустам'
                else:
                    # Убираем падежные окончания для новых имен
                    if name.endswith('у') or name.endswith('а') or name.endswith('е'):
                        params['name'] = name[:-1]
                    else:
                        params['name'] = name
                break

    # Для других команд - общий поиск имен
    if 'name' not in params:
        # Ищем любые имена с большой буквы
        names = re.findall(r'\b[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?\b', text)
        if names:
            name = names[0]
            # Убираем падежные окончания
            if name.endswith('у') or name.endswith('а') or name.endswith('е'):
                params['name'] = name[:-1]
            else:
                params['name'] = name

    # Извлекаем периоды
    if any(word in text_lower for word in ['неделя', 'неделю']):
        params['period'] = 'неделя'
    elif any(word in text_lower for word in ['месяц']):
        params['period'] = 'месяц'
    elif any(word in text_lower for word in ['декабрь', 'январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь']):
        months = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь']
        for month in months:
            if month in text_lower:
                params['period'] = month
                break

    # Извлекаем категории
    if any(word in text_lower for word in ['зарплат', 'зарплаты']):
        params['category'] = 'зарплаты'
    elif any(word in text_lower for word in ['поставщик', 'поставщиков']):
        params['category'] = 'поставщик'
    elif any(word in text_lower for word in ['процент', 'проценты']):
        params['category'] = 'процент'

    return params

def create_quick_buttons():
    """Создает быстрые кнопки для частых действий"""
    keyboard = [
        [
            InlineKeyboardButton("📊 Отчет", callback_data="quick_analytics"),
            InlineKeyboardButton("🔍 Поиск", callback_data="quick_search")
        ],
        [
            InlineKeyboardButton("📋 История", callback_data="quick_history"),
            InlineKeyboardButton("💾 Бэкап", callback_data="quick_backup")
        ],
        [
            InlineKeyboardButton("📂 Категории", callback_data="quick_categories"),
            InlineKeyboardButton("👥 Получатели", callback_data="quick_recipients")
        ],
        [
            InlineKeyboardButton("🏭 Поставщики", callback_data="quick_suppliers")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_search_buttons():
    """Создает кнопки для популярных поисковых запросов"""
    keyboard = [
        [
            InlineKeyboardButton("👥 Петров", callback_data="search_петров"),
            InlineKeyboardButton("🏭 Интигам", callback_data="search_интигам")
        ],
        [
            InlineKeyboardButton("💰 Зарплаты", callback_data="search_зарплаты"),
            InlineKeyboardButton("📊 Процент", callback_data="search_процент")
        ],
        [
            InlineKeyboardButton("📅 За неделю", callback_data="search_неделя"),
            InlineKeyboardButton("💸 >50000", callback_data="search_>50000")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик голосовых сообщений"""
    if not is_allowed_user(update):
        await update.message.reply_text('Нет доступа')
        return
    try:
        user_id = update.effective_user.id

        # Показываем что бот обрабатывает голосовое
        await update.message.reply_text("🎤 Распознаю голосовое сообщение...")

        # Получаем файл голосового сообщения
        voice_file = await context.bot.get_file(update.message.voice.file_id)

        # Скачиваем файл
        voice_path = f"voice_{update.message.voice.file_id}.ogg"
        await voice_file.download_to_drive(voice_path)

        # Конвертируем в текст через Whisper
        with open(voice_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="ru"
            )

        # Удаляем временный файл
        os.remove(voice_path)

        recognized_text = transcript.text

        # Показываем что распознали
        await update.message.reply_text(f"📝 Распознал: \"{recognized_text}\"")

        # Обрабатываем с контекстом
        user_context = USER_CONTEXT.get(user_id)
        analysis = analyze_message_with_ai(recognized_text, user_context)

        await process_analysis_result(update, analysis, user_id, f"🎤 \"{recognized_text}\"", context)

    except Exception as e:
        logger.error(f"Ошибка обработки голосового: {e}")
        await update.message.reply_text("❌ Ошибка при обработке голосового сообщения.")

async def handle_voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE, analysis):
    """Обрабатывает голосовые команды"""
    command = analysis["command"]
    params_text = analysis["params"]
    params = extract_params_from_voice(params_text, command)

    # Получаем message объект
    message = update.message if update.message else update.callback_query.message

    if command == "analytics":
        await show_analytics(update, context)

    elif command == "search":
        # Формируем поисковый запрос из параметров
        search_terms = []
        if 'name' in params:
            search_terms.append(params['name'])
        if 'period' in params:
            search_terms.append(params['period'])
        if 'category' in params:
            search_terms.append(params['category'])

        if search_terms:
            # Имитируем команду search
            context.args = search_terms
            await advanced_search(update, context)
        else:
            await message.reply_text(
                "🔍 **Голосовой поиск**\n\nПопробуйте сказать:\n• 'Найди Петрова'\n• 'Покажи операции за неделю'\n• 'Когда платили Интигаму'",
                reply_markup=create_search_buttons()
            )

    elif command == "categories":
        period = params.get('period', None)
        if period:
            context.args = [period]
        else:
            context.args = []
        await category_analysis(update, context)

    elif command == "suppliers":
        if 'name' in params:
            context.args = [params['name']]
            await supplier_analysis(update, context)
        else:
            await message.reply_text("🏭 Назовите поставщика для анализа.\nНапример: 'Анализ поставщика Интигам'")

    elif command == "recipients":
        period = params.get('period', None)
        if period:
            context.args = [period]
        else:
            context.args = []
        await description_analysis(update, context)

    elif command == "history":
        await show_context_history(update, context)

    elif command == "backup":
        await create_backup(update, context)

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия на кнопки"""
    if not is_allowed_user(update):
        await update.callback_query.answer()
        await update.callback_query.edit_message_text('Нет доступа')
        return
    query = update.callback_query
    await query.answer()

    data = query.data

    # Быстрые команды
    if data == "quick_analytics":
        await show_analytics(update, context)

    elif data == "quick_search":
        await query.edit_message_text(
            "🔍 **Быстрый поиск**\n\nВыберите категорию или скажите что ищете:",
            reply_markup=create_search_buttons()
        )

    elif data == "quick_history":
        await show_context_history(update, context)

    elif data == "quick_backup":
        await create_backup(update, context)

    elif data == "quick_categories":
        context.args = []
        await category_analysis(update, context)

    elif data == "quick_recipients":
        context.args = []
        await description_analysis(update, context)

    elif data == "quick_suppliers":
        await query.edit_message_text(
            "🏭 **Анализ поставщиков**\n\nСкажите: 'Анализ поставщика [название]'\nНапример: 'Анализ поставщика Интигам'"
        )

    # Поисковые запросы
    elif data.startswith("search_"):
        search_term = data.replace("search_", "")
        context.args = [search_term]
        await advanced_search(update, context)

async def process_analysis_result(update, analysis, user_id, source_info="", context=None):
    """Обрабатывает результат анализа ИИ"""
    if not is_allowed_user(update):
        await update.message.reply_text('Нет доступа')
        return

    # Обрабатываем голосовые команды
    if analysis["type"] == "voice_command":
        await handle_voice_command(update, context, analysis)
        return

    if analysis["type"] == "finance":
        confidence = analysis.get('confidence', 1.0)

        # Если уверенность низкая - запрашиваем подтверждение
        if confidence < 0.7:
            confirm_text = f"""
❓ **Проверьте правильность:**

{source_info}
🔄 Тип: {analysis['operation_type']}
📂 Категория: {analysis['category']}
📝 Описание: {analysis['description']}
💰 Сумма: {analysis['amount']:,.0f} ₽

✅ Записать? Или уточните что не так.
            """
            await update.message.reply_text(confirm_text, parse_mode='Markdown')
            return

        # Записываем операцию
        if add_finance_record(analysis, user_id):
            emoji = "📈" if analysis["operation_type"] == "Пополнение" else "📉"
            response = f"""
{emoji} **Финансовая операция записана:**

{source_info}
📅 Дата: {format_moscow_date()}
🔄 Тип: {analysis['operation_type']}
📂 Категория: {analysis['category']}
📝 Описание: {analysis['description']}
💰 Сумма: {analysis['amount']:,.0f} ₽

✅ **Записано в Google Таблицу!**
            """

            # Добавляем быстрые кнопки после записи операции
            await update.message.reply_text(
                response,
                parse_mode='Markdown',
                reply_markup=create_quick_buttons()
            )
        else:
            await update.message.reply_text("❌ Ошибка при записи в таблицу финансов.")

    else:  # clarification
        suggestions = analysis.get('suggestions', [])
        response = f"❓ {analysis.get('message', 'Не понял ваше сообщение.')}"

        if suggestions:
            response += "\n\n💡 **Возможно, вы имели в виду:**\n"
            for i, suggestion in enumerate(suggestions[:3], 1):
                response += f"{i}. {suggestion}\n"

        await update.message.reply_text(response, parse_mode='Markdown')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    welcome_text = """
💰 **Умный финансовый помощник с ИИ!**

🎤 **Новинка: Голосовое управление!**

💸 **Записывайте операции:**
• "Дал Петрову 40000 за работу"
• "Таня лично 30000"
• "Оплатил поставщику Интигаму 300000"

🗣️ **Управляйте голосом:**
• 🎤 "Покажи траты за неделю"
• 🎤 "Найди все операции с Петровым"
• 🎤 "Анализ по категориям за месяц"
• 🎤 "Когда платили Интигаму"

🏭 **11 категорий:**
• Зарплаты, Учредители, Поставщики
• Процент, Закупка товара, Материалы
• Транспорт, Связь, Такси, Общественные, СВО

**Говорите естественно - бот всё поймет!**
    """

    await update.message.reply_text(
        welcome_text,
        parse_mode='Markdown',
        reply_markup=create_quick_buttons()
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений с контекстом"""
    if not is_allowed_user(update):
        await update.message.reply_text('Нет доступа')
        return
    user_id = update.effective_user.id
    user_message = update.message.text

    # Показываем что бот думает
    await update.message.reply_text("🤔 Анализирую с учетом контекста...")

    # Анализируем с контекстом
    user_context = USER_CONTEXT.get(user_id)
    analysis = analyze_message_with_ai(user_message, user_context)

    await process_analysis_result(update, analysis, user_id, context=context)

async def show_context_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает историю с контекстом"""
    if not is_allowed_user(update):
        await update.message.reply_text('Нет доступа')
        return
    user_id = update.effective_user.id
    message = update.message if update.message else update.callback_query.message

    try:
        await message.reply_text("📊 Получаю историю с контекстом...")

        # История из контекста
        user_context = USER_CONTEXT.get(user_id, {})
        recent_ops = user_context.get('recent_operations', [])

        if recent_ops:
            history = "🧠 **Контекст последних операций:**\n\n"
            for i, op in enumerate(reversed(recent_ops[-5:]), 1):
                history += f"{i}. {op}\n"
        else:
            history = "📊 **Контекст пуст** - начните добавлять операции!\n\n"

        # Последние из таблицы
        finance_records = finance_sheet.get_all_records()
        recent_finance = finance_records[-3:] if len(finance_records) > 3 else finance_records

        if recent_finance:
            history += "\n💰 **Последние финансовые операции:**\n"
            for record in reversed(recent_finance):
                emoji = "📈" if record.get('Сумма', 0) > 0 else "📉"
                history += f"{emoji} {record.get('Описание/Получатель', '')}: {record.get('Сумма', 0):,.0f} ₽\n"

        await message.reply_text(history, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Ошибка истории: {e}")
        await message.reply_text("❌ Ошибка при получении истории.")

async def show_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Умная аналитика трат"""
    if not is_allowed_user(update):
        await update.message.reply_text('Нет доступа')
        return
    try:
        # Получаем message объект правильно
        message = update.message if update.message else update.callback_query.message

        await message.reply_text("📊 Анализирую ваши финансы...")

        # Получаем данные за последний месяц
        month_ago = datetime.now() - timedelta(days=30)
        finance_records = finance_sheet.get_all_records()

        recent_records = []
        for record in finance_records:
            try:
                record_date = datetime.strptime(record.get('Дата', ''), '%d.%m.%Y')
                if record_date >= month_ago:
                    recent_records.append(record)
            except:
                continue

        if not recent_records:
            await message.reply_text("📊 Недостаточно данных для аналитики.")
            return

        # Анализируем
        total_income = sum(record.get('Сумма', 0) for record in recent_records if record.get('Сумма', 0) > 0)
        total_expense = sum(record.get('Сумма', 0) for record in recent_records if record.get('Сумма', 0) < 0)

        # По категориям
        categories = {}
        for record in recent_records:
            if record.get('Сумма', 0) < 0:
                cat = record.get('Категория', 'Прочее')
                categories[cat] = categories.get(cat, 0) + record.get('Сумма', 0)

        # Самые частые получатели зарплат
        salaries = {}
        for record in recent_records:
            if record.get('Категория') == 'Зарплаты сотрудникам':
                person = record.get('Описание/Получатель', 'Неизвестно')
                salaries[person] = salaries.get(person, 0) + abs(record.get('Сумма', 0))

        report = f"""
📊 **Умная аналитика за 30 дней**

💰 **Общие итоги:**
📈 Доходы: +{total_income:,.0f} ₽
📉 Расходы: {total_expense:,.0f} ₽
💼 Чистый результат: {total_income + total_expense:,.0f} ₽
📊 Операций: {len(recent_records)}

💸 **Расходы по категориям:**
"""

        for cat, amount in sorted(categories.items(), key=lambda x: x[1]):
            percent = abs(amount) / abs(total_expense) * 100 if total_expense != 0 else 0
            report += f"• {cat}: {amount:,.0f} ₽ ({percent:.1f}%)\n"

        if salaries:
            report += f"\n👥 **Зарплаты сотрудникам:**\n"
            for person, amount in sorted(salaries.items(), key=lambda x: x[1], reverse=True):
                report += f"• {person}: {amount:,.0f} ₽\n"

        # Средние траты
        avg_daily = abs(total_expense) / 30
        report += f"\n📈 **Средние траты в день:** {avg_daily:,.0f} ₽"

        # Найти самую затратную категорию
        if categories:
            top_category = min(categories.items(), key=lambda x: x[1])
            report += f"\n🔝 **Больше всего тратите на:** {top_category[0]}"

        await message.reply_text(report, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Ошибка аналитики: {e}")
        message = update.message if update.message else update.callback_query.message
        await message.reply_text("❌ Ошибка при создании аналитики.")

async def description_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Анализ трат по описанию (кому больше всего платите)"""
    if not is_allowed_user(update):
        await update.message.reply_text('Нет доступа')
        return
    args = context.args
    message = update.message if update.message else update.callback_query.message

    try:
        await message.reply_text("👥 Анализирую траты по получателям...")

        finance_records = finance_sheet.get_all_records()

        # Определяем период
        if args and args[0] in ['месяц', 'неделя']:
            if args[0] == 'месяц':
                cutoff_date = datetime.now() - timedelta(days=30)
                period_name = "месяц"
            else:
                cutoff_date = datetime.now() - timedelta(days=7)
                period_name = "неделю"

            filtered_records = []
            for record in finance_records:
                try:
                    record_date = datetime.strptime(record.get('Дата', ''), '%d.%m.%Y')
                    if record_date >= cutoff_date:
                        filtered_records.append(record)
                except:
                    continue
        else:
            filtered_records = finance_records
            period_name = "все время"

        # Группируем по описанию (получателям)
        recipients = {}
        total_expense = 0

        for record in filtered_records:
            amount = record.get('Сумма', 0)
            if amount < 0:  # Только расходы
                description = record.get('Описание/Получатель', 'Без описания').strip()
                category = record.get('Категория', 'Прочее')

                if description and description != 'Без описания':
                    recipients[description] = recipients.get(description, {
                        'total': 0,
                        'count': 0,
                        'categories': {}
                    })
                    recipients[description]['total'] += abs(amount)
                    recipients[description]['count'] += 1
                    recipients[description]['categories'][category] = recipients[description]['categories'].get(category, 0) + abs(amount)
                    total_expense += abs(amount)

        if not recipients:
            await message.reply_text("👥 Нет данных о получателях за выбранный период.")
            return

        # Сортируем по убыванию суммы
        sorted_recipients = sorted(recipients.items(), key=lambda x: x[1]['total'], reverse=True)

        result = f"👥 **Анализ трат по получателям за {period_name}**\n\n"
        result += f"💰 **Общие расходы:** {total_expense:,.0f} ₽\n"
        result += f"👤 **Уникальных получателей:** {len(recipients)}\n\n"

        # Топ получателей
        result += "🔝 **Топ получателей:**\n"
        for i, (recipient, data) in enumerate(sorted_recipients[:10], 1):
            percentage = (data['total'] / total_expense) * 100
            avg_payment = data['total'] / data['count']

            # Определяем основную категорию
            main_category = max(data['categories'].items(), key=lambda x: x[1])[0]

            # Эмодзи по категориям
            emoji_map = {
                'Зарплаты сотрудникам': '👨‍💼',
                'Выплаты учредителям': '👔',
                'Оплата поставщику': '🏭',
                'Процент': '📊',
                'Закупка товара': '🛒',
                'Транспорт': '🚗',
                'Такси': '🚕',
                'Связь': '📱',
                'Материалы': '📦',
                'Общественные расходы': '🏢',
                'Благотворительность': '❤️'
            }

            emoji = emoji_map.get(main_category, '💰')

            result += f"{i}. {emoji} **{recipient}**\n"
            result += f"   💰 {data['total']:,.0f} ₽ ({percentage:.1f}%)\n"
            result += f"   📊 {data['count']} операций, ~{avg_payment:,.0f} ₽ за раз\n"
            result += f"   📂 Основная категория: {main_category}\n\n"

        # Статистика
        if len(sorted_recipients) > 10:
            others_total = sum(data['total'] for _, data in sorted_recipients[10:])
            others_count = len(sorted_recipients) - 10
            result += f"... и ещё {others_count} получателей на {others_total:,.0f} ₽\n\n"

        # Топ-3 анализ
        if len(sorted_recipients) >= 3:
            top3_total = sum(data['total'] for _, data in sorted_recipients[:3])
            top3_percentage = (top3_total / total_expense) * 100
            result += f"📈 **Топ-3 получателя:** {top3_percentage:.1f}% от всех трат\n"

        # Средний чек по категориям
        category_avg = {}
        for recipient, data in recipients.items():
            for category, amount in data['categories'].items():
                if category not in category_avg:
                    category_avg[category] = []
                category_avg[category].append(amount / recipients[recipient]['count'])

        if category_avg:
            result += f"\n💳 **Средний чек по типам:**\n"
            for category, amounts in category_avg.items():
                avg = sum(amounts) / len(amounts)
                result += f"• {category}: {avg:,.0f} ₽\n"

        await message.reply_text(result, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Ошибка анализа по описанию: {e}")
        await message.reply_text("❌ Ошибка при анализе получателей.")

async def advanced_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Продвинутый поиск операций"""
    if not is_allowed_user(update):
        await update.message.reply_text('Нет доступа')
        return
    args = context.args
    message = update.message if update.message else update.callback_query.message

    if not args:
        help_text = """
🔍 **Супер-поиск операций:**

**По имени/компании:**
• `/search Петров` - все операции с Петровым
• `/search Интигам` - все операции с Интигамом
• `/search ООО` - все операции с ООО

**По категории:**
• `/search зарплаты` - все зарплаты
• `/search поставщик` - все оплаты поставщикам
• `/search материалы` - все покупки материалов

**По периоду:**
• `/search декабрь` - операции за декабрь
• `/search 2024` - операции за 2024 год
• `/search неделя` - операции за неделю

**По сумме:**
• `/search >50000` - операции больше 50к
• `/search <10000` - операции меньше 10к
• `/search 25000` - точная сумма

**Комбинированный поиск:**
• `/search Петров декабрь` - зарплаты Петрову в декабре
• `/search поставщик >100000` - крупные оплаты поставщикам
        """
        await message.reply_text(help_text, parse_mode='Markdown')
        return

    search_query = " ".join(args).lower()

    try:
        await message.reply_text(f"🔍 Ищу операции по запросу: '{search_query}'...")

        finance_records = finance_sheet.get_all_records()
        found_records = []

        # Анализируем поисковый запрос
        filters = parse_search_query(search_query)

        for record in finance_records:
            if matches_filters(record, filters):
                found_records.append(record)

        if not found_records:
            await message.reply_text(f"❌ По запросу '{search_query}' ничего не найдено.")
            return

        # Сортируем по дате (новые сверху)
        found_records = sorted(found_records, key=lambda x: datetime.strptime(x.get('Дата', '01.01.2000'), '%d.%m.%Y'), reverse=True)

        # Формируем результат
        result = f"🔍 **Найдено: {len(found_records)} операций**\n"
        result += f"📊 **Запрос:** {search_query}\n\n"

        # Группируем результаты
        if len(found_records) > 15:
            result += "📋 **Последние 15 операций:**\n"
            display_records = found_records[:15]
        else:
            display_records = found_records

        for record in display_records:
            emoji = "📈" if record.get('Сумма', 0) > 0 else "📉"
            category = record.get('Категория', 'Прочее')
            date = record.get('Дата', '')
            description = record.get('Описание/Получатель', '')
            amount = record.get('Сумма', 0)

            result += f"{emoji} {date}: {description} - {amount:,.0f} ₽ ({category})\n"

        if len(found_records) > 15:
            result += f"\n... и ещё {len(found_records) - 15} операций"

        # Аналитика результатов
        total_amount = sum(record.get('Сумма', 0) for record in found_records)
        income = sum(record.get('Сумма', 0) for record in found_records if record.get('Сумма', 0) > 0)
        expense = sum(record.get('Сумма', 0) for record in found_records if record.get('Сумма', 0) < 0)

        result += f"\n\n📊 **Итоги поиска:**\n"
        result += f"💰 Общая сумма: {total_amount:,.0f} ₽\n"
        if income > 0:
            result += f"📈 Доходы: +{income:,.0f} ₽\n"
        if expense < 0:
            result += f"📉 Расходы: {expense:,.0f} ₽\n"

        # Топ категории в результатах
        categories = {}
        for record in found_records:
            cat = record.get('Категория', 'Прочее')
            categories[cat] = categories.get(cat, 0) + abs(record.get('Сумма', 0))

        if len(categories) > 1:
            top_category = max(categories.items(), key=lambda x: x[1])
            result += f"🔝 Топ категория: {top_category[0]} ({top_category[1]:,.0f} ₽)"

        await message.reply_text(result, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Ошибка продвинутого поиска: {e}")
        await message.reply_text("❌ Ошибка при поиске операций.")

def parse_search_query(query):
    """Парсит поисковый запрос и извлекает фильтры"""
    filters = {
        'text': [],
        'categories': [],
        'amount_min': None,
        'amount_max': None,
        'amount_exact': None,
        'period': None
    }

    tokens = query.split()

    for token in tokens:
        # Поиск по сумме
        if token.startswith('>'):
            try:
                filters['amount_min'] = float(token[1:])
                continue
            except:
                pass

        if token.startswith('<'):
            try:
                filters['amount_max'] = float(token[1:])
                continue
            except:
                pass

        # Точная сумма
        if token.isdigit():
            filters['amount_exact'] = float(token)
            continue

        # Категории
        if token in ['зарплат', 'зарплаты', 'зарплата']:
            filters['categories'].append('Зарплаты сотрудникам')
        elif token in ['поставщик', 'поставщику', 'поставщиков']:
            filters['categories'].append('Оплата поставщику')
        elif token in ['материал', 'материалы']:
            filters['categories'].append('Материалы')
        elif token in ['такси']:
            filters['categories'].append('Такси')
        elif token in ['транспорт']:
            filters['categories'].append('Транспорт')
        elif token in ['связь']:
            filters['categories'].append('Связь')
        elif token in ['благотворительность', 'сво']:
            filters['categories'].append('Благотворительность')
        elif token in ['общественн', 'хоз', 'хозяйственные']:
            filters['categories'].append('Общественные расходы')
        elif token in ['учредител', 'учредители', 'лично']:
            filters['categories'].append('Выплаты учредителям')

        # Периоды
        elif token in ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь',
                      'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь']:
            filters['period'] = token
        elif token in ['неделя', 'неделю']:
            filters['period'] = 'week'
        elif token in ['месяц']:
            filters['period'] = 'month'
        elif token == '2024' or token == '2025':
            filters['period'] = token

        # Обычный текстовый поиск
        else:
            filters['text'].append(token)

    return filters

def matches_filters(record, filters):
    """Проверяет соответствие записи фильтрам"""

    # Текстовый поиск
    if filters['text']:
        text_to_search = f"{record.get('Описание/Получатель', '')} {record.get('Категория', '')}".lower()
        for text_filter in filters['text']:
            if text_filter not in text_to_search:
                return False

    # Фильтр по категориям
    if filters['categories']:
        if record.get('Категория', '') not in filters['categories']:
            return False

    # Фильтр по сумме
    amount = abs(record.get('Сумма', 0))

    if filters['amount_min'] is not None:
        if amount < filters['amount_min']:
            return False

    if filters['amount_max'] is not None:
        if amount > filters['amount_max']:
            return False

    if filters['amount_exact'] is not None:
        if amount != filters['amount_exact']:
            return False

    # Фильтр по периоду
    if filters['period']:
        record_date_str = record.get('Дата', '')
        if record_date_str:
            try:
                record_date = datetime.strptime(record_date_str, '%d.%m.%Y')

                if filters['period'] == 'week':
                    week_ago = datetime.now() - timedelta(days=7)
                    if record_date < week_ago:
                        return False

                elif filters['period'] == 'month':
                    month_ago = datetime.now() - timedelta(days=30)
                    if record_date < month_ago:
                        return False

                elif filters['period'] in ['2024', '2025']:
                    if record_date.year != int(filters['period']):
                        return False

                elif filters['period'] in ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь',
                                         'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь']:
                    months = {
                        'январь': 1, 'февраль': 2, 'март': 3, 'апрель': 4,
                        'май': 5, 'июнь': 6, 'июль': 7, 'август': 8,
                        'сентябрь': 9, 'октябрь': 10, 'ноябрь': 11, 'декабрь': 12
                    }
                    if record_date.month != months[filters['period']]:
                        return False
            except:
                return False

    return True

async def category_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Анализ по категориям"""
    if not is_allowed_user(update):
        await update.message.reply_text('Нет доступа')
        return
    args = context.args
    message = update.message if update.message else update.callback_query.message

    try:
        await message.reply_text("📊 Анализирую категории...")

        finance_records = finance_sheet.get_all_records()

        # Определяем период
        if args and args[0] in ['месяц', 'неделя']:
            if args[0] == 'месяц':
                cutoff_date = datetime.now() - timedelta(days=30)
                period_name = "месяц"
            else:
                cutoff_date = datetime.now() - timedelta(days=7)
                period_name = "неделю"

            filtered_records = []
            for record in finance_records:
                try:
                    record_date = datetime.strptime(record.get('Дата', ''), '%d.%m.%Y')
                    if record_date >= cutoff_date:
                        filtered_records.append(record)
                except:
                    continue
        else:
            filtered_records = finance_records
            period_name = "все время"

        # Группируем по категориям
        categories = {}
        total_expense = 0

        for record in filtered_records:
            amount = record.get('Сумма', 0)
            if amount < 0:  # Только расходы
                category = record.get('Категория', 'Прочее')
                categories[category] = categories.get(category, 0) + abs(amount)
                total_expense += abs(amount)

        if not categories:
            await message.reply_text("📊 Нет данных о расходах за выбранный период.")
            return

        # Сортируем по убыванию
        sorted_categories = sorted(categories.items(), key=lambda x: x[1], reverse=True)

        result = f"📊 **Анализ расходов за {period_name}**\n\n"
        result += f"💰 **Общие расходы:** {total_expense:,.0f} ₽\n\n"

        for i, (category, amount) in enumerate(sorted_categories, 1):
            percentage = (amount / total_expense) * 100
            bar_length = int(percentage / 5)  # Шкала из 20 символов
            bar = "█" * bar_length + "░" * (20 - bar_length)

            result += f"{i}. **{category}**\n"
            result += f"   💰 {amount:,.0f} ₽ ({percentage:.1f}%)\n"
            result += f"   {bar}\n\n"

        # Топ-3 категории
        if len(sorted_categories) >= 3:
            top3_total = sum(amount for _, amount in sorted_categories[:3])
            top3_percentage = (top3_total / total_expense) * 100
            result += f"🔝 **Топ-3 категории:** {top3_percentage:.1f}% от всех трат"

        await message.reply_text(result, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Ошибка анализа категорий: {e}")
        await message.reply_text("❌ Ошибка при анализе категорий.")

async def supplier_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Анализ поставщиков"""
    if not is_allowed_user(update):
        await update.message.reply_text('Нет доступа')
        return
    args = context.args
    message = update.message if update.message else update.callback_query.message

    if not args:
        await message.reply_text("🏭 Использование: /suppliers [название]\nПример: /suppliers Интигам")
        return

    supplier_name = " ".join(args).lower()

    try:
        await message.reply_text(f"🏭 Анализирую операции с поставщиком '{supplier_name}'...")

        finance_records = finance_sheet.get_all_records()
        supplier_records = []

        for record in finance_records:
            if (record.get('Категория', '') == 'Оплата поставщику' and
                supplier_name in record.get('Описание/Получатель', '').lower()):
                supplier_records.append(record)

        if not supplier_records:
            await message.reply_text(f"❌ Операции с поставщиком '{supplier_name}' не найдены.")
            return

        # Сортируем по дате
        supplier_records = sorted(supplier_records, key=lambda x: datetime.strptime(x.get('Дата', '01.01.2000'), '%d.%m.%Y'))

        total_paid = sum(abs(record.get('Сумма', 0)) for record in supplier_records)

        result = f"🏭 **Анализ поставщика: {supplier_name.title()}**\n\n"
        result += f"📊 **Всего операций:** {len(supplier_records)}\n"
        result += f"💰 **Общая сумма:** {total_paid:,.0f} ₽\n\n"

        if len(supplier_records) > 0:
            avg_amount = total_paid / len(supplier_records)
            result += f"📈 **Средняя оплата:** {avg_amount:,.0f} ₽\n"

            # Последние операции
            result += f"\n📋 **Последние операции:**\n"
            for record in supplier_records[-5:]:
                date = record.get('Дата', '')
                amount = abs(record.get('Сумма', 0))
                result += f"• {date}: {amount:,.0f} ₽\n"

            # Частота операций
            if len(supplier_records) > 1:
                first_date = datetime.strptime(supplier_records[0].get('Дата', ''), '%d.%m.%Y')
                last_date = datetime.strptime(supplier_records[-1].get('Дата', ''), '%d.%m.%Y')
                days_span = (last_date - first_date).days

                if days_span > 0:
                    frequency = len(supplier_records) / (days_span / 30)  # операций в месяц
                    result += f"\n📅 **Частота:** {frequency:.1f} операций в месяц"

        await message.reply_text(result, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Ошибка анализа поставщика: {e}")
        await message.reply_text("❌ Ошибка при анализе поставщика.")

async def find_operations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Поиск операций"""
    if not is_allowed_user(update):
        await update.message.reply_text('Нет доступа')
        return
    args = context.args
    message = update.message if update.message else update.callback_query.message

    if not args:
        await message.reply_text("🔍 Использование: /find [имя или ключевое слово]\nПример: /find Петров")
        return

    search_term = " ".join(args).lower()

    try:
        await message.reply_text(f"🔍 Ищу операции с '{search_term}'...")

        finance_records = finance_sheet.get_all_records()
        found_records = []

        for record in finance_records:
            description = str(record.get('Описание/Получатель', '')).lower()
            category = str(record.get('Категория', '')).lower()
            if search_term in description or search_term in category:
                found_records.append(record)

        if not found_records:
            await message.reply_text(f"❌ Операции с '{search_term}' не найдены.")
            return

        result = f"🔍 **Найдено операций с '{search_term}': {len(found_records)}**\n\n"

        # Показываем последние 10
        for record in found_records[-10:]:
            emoji = "📈" if record.get('Сумма', 0) > 0 else "📉"
            result += f"{emoji} {record.get('Дата', '')}: {record.get('Описание/Получатель', '')} - {record.get('Сумма', 0):,.0f} ₽\n"

        if len(found_records) > 10:
            result += f"\n... и ещё {len(found_records) - 10} операций"

        # Итоги
        total_amount = sum(record.get('Сумма', 0) for record in found_records)
        result += f"\n\n💰 **Общая сумма:** {total_amount:,.0f} ₽"

        await message.reply_text(result, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Ошибка поиска: {e}")
        await message.reply_text("❌ Ошибка при поиске операций.")

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает главное меню с кнопками"""
    if not is_allowed_user(update):
        await update.message.reply_text('Нет доступа')
        return
    menu_text = """
🎛️ **Главное меню финансового бота**

Выберите действие:
    """

    await update.message.reply_text(
        menu_text,
        parse_mode='Markdown',
        reply_markup=create_quick_buttons()
    )

async def show_analytics_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает меню аналитики"""
    if not is_allowed_user(update):
        await update.message.reply_text('Нет доступа')
        return
    keyboard = [
        [
            InlineKeyboardButton("📊 Общий отчет", callback_data="quick_analytics"),
            InlineKeyboardButton("📂 По категориям", callback_data="quick_categories")
        ],
        [
            InlineKeyboardButton("👥 По получателям", callback_data="quick_recipients"),
            InlineKeyboardButton("🏭 Поставщики", callback_data="quick_suppliers")
        ],
        [
            InlineKeyboardButton("🔍 Поиск", callback_data="quick_search"),
            InlineKeyboardButton("📋 История", callback_data="quick_history")
        ]
    ]

    await update.message.reply_text(
        "📊 **Меню аналитики**\n\nВыберите тип анализа:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def delete_last_operation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет последнюю операцию"""
    if not is_allowed_user(update):
        await update.message.reply_text('Нет доступа')
        return
    user_id = update.effective_user.id
    message = update.message if update.message else update.callback_query.message

    if user_id not in USER_LAST_OPERATIONS:
        await message.reply_text("❌ Нет операций для удаления.")
        return

    try:
        last_op = USER_LAST_OPERATIONS[user_id]

        # Проверяем что операция не старше 1 часа
        moscow_now = get_moscow_time()
        if (moscow_now - last_op['timestamp']).seconds > 3600:
            await message.reply_text("❌ Можно удалить только операции за последний час.")
            return

        # Удаляем строку из таблицы
        finance_sheet.delete_rows(last_op['row'])
        op_info = f"💰 {last_op['data']['description']}: {last_op['data']['amount']:,.0f} ₽"

        # Удаляем из памяти
        del USER_LAST_OPERATIONS[user_id]

        await message.reply_text(f"✅ **Операция удалена:**\n{op_info}", parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Ошибка удаления: {e}")
        await message.reply_text("❌ Ошибка при удалении операции.")

async def create_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создает резервную копию данных"""
    if not is_allowed_user(update):
        await update.message.reply_text('Нет доступа')
        return
    message = update.message if update.message else update.callback_query.message

    try:
        await message.reply_text("💾 Создаю резервную копию...")

        # Получаем все данные
        finance_records = finance_sheet.get_all_records()

        backup_data = {
            'created': get_moscow_time().strftime('%d.%m.%Y %H:%M'),
            'finance_records': len(finance_records),
            'finance': finance_records
        }

        # Создаем файл
        backup_filename = f"backup_{get_moscow_time().strftime('%Y%m%d_%H%M')}.json"
        with open(backup_filename, 'w', encoding='utf-8') as f:
            json.dump(backup_data, f, ensure_ascii=False, indent=2)

        # Отправляем файл пользователю
        with open(backup_filename, 'rb') as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=f,
                filename=backup_filename,
                caption=f"💾 **Резервная копия создана!**\n\n📊 Финансовых записей: {len(finance_records)}\n📅 Дата: {backup_data['created']}"
            )

        # Удаляем временный файл
        os.remove(backup_filename)

    except Exception as e:
        logger.error(f"Ошибка создания backup: {e}")
        await message.reply_text("❌ Ошибка при создании резервной копии.")

async def clear_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очищает все данные из таблиц"""
    if not is_allowed_user(update):
        await update.message.reply_text('Нет доступа')
        return
    message = update.message if update.message else update.callback_query.message

    try:
        # Очищаем финансовые данные (оставляем только заголовки)
        finance_sheet.clear()
        finance_sheet.append_row(["Дата", "Тип операции", "Категория", "Описание/Получатель", "Сумма", "Комментарий"])

        # Очищаем контекст пользователя
        user_id = update.effective_user.id
        if user_id in USER_CONTEXT:
            del USER_CONTEXT[user_id]
        if user_id in USER_LAST_OPERATIONS:
            del USER_LAST_OPERATIONS[user_id]

        response = """
🗑️ **Все данные очищены!**

✅ Финансовые записи удалены
✅ Контекст очищен
✅ Заголовки восстановлены

Можете начинать заново!
        """

        await message.reply_text(response, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Ошибка очистки данных: {e}")
        await message.reply_text("❌ Ошибка при очистке данных.")

async def reset_sheets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пересоздает структуру таблиц"""
    if not is_allowed_user(update):
        await update.message.reply_text('Нет доступа')
        return
    message = update.message if update.message else update.callback_query.message

    try:
        # Пересоздаем заголовки на всякий случай
        finance_sheet.clear()
        finance_sheet.append_row(["Дата", "Тип операции", "Категория", "Описание/Получатель", "Сумма", "Комментарий"])

        await message.reply_text("✅ Структура таблиц восстановлена!")

    except Exception as e:
        logger.error(f"Ошибка восстановления структуры: {e}")
        await message.reply_text("❌ Ошибка при восстановлении структуры таблиц.")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    if update and not is_allowed_user(update):
        if hasattr(update, 'message') and update.message:
            await update.message.reply_text('Нет доступа')
        elif hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text('Нет доступа')
        return
    logger.error(f"Ошибка: {context.error}")

def main():
    """Запуск продвинутого ИИ-бота"""
    print("🚀 Запускаю продвинутый ИИ финансовый бот...")

    # Создаем приложение
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Добавляем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", show_menu))
    application.add_handler(CommandHandler("analytics_menu", show_analytics_menu))
    application.add_handler(CommandHandler("search", advanced_search))
    application.add_handler(CommandHandler("categories", category_analysis))
    application.add_handler(CommandHandler("recipients", description_analysis))
    application.add_handler(CommandHandler("suppliers", supplier_analysis))
    application.add_handler(CommandHandler("history", show_context_history))
    application.add_handler(CommandHandler("analytics", show_analytics))
    application.add_handler(CommandHandler("find", find_operations))
    application.add_handler(CommandHandler("delete", delete_last_operation))
    application.add_handler(CommandHandler("backup", create_backup))
    application.add_handler(CommandHandler("clear", clear_data))
    application.add_handler(CommandHandler("reset", reset_sheets))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)

    # Запускаем бота
    print("🧠 Продвинутый ИИ-бот готов к работе!")
    print("🎤 Поддержка голосовых сообщений включена!")
    print("🧠 Контекстное понимание активировано!")
    print("📊 Умная аналитика доступна!")
    print("🔍 Продвинутый поиск включен!")
    print("")
    print("📊 Доступные команды:")
    print("   /start - приветствие и обзор возможностей")
    print("   /history - история операций с контекстом")
    print("   /analytics - умная аналитика трат")
    print("   /find [слово] - поиск операций")
    print("   /delete - удалить последнюю операцию")
    print("   /backup - создать резервную копию")
    print("   /clear - очистить все данные")
    print("   /reset - восстановить структуру таблиц")
    print("")
    print("💡 Говорите естественно - бот понимает контекст!")
    print("💡 Используйте фразы типа 'такая же сумма', 'тому же человеку'")

    # Запускаем приложение
    application.run_polling(allowed_updates=Update.ALL_TYPES)

    # --- В main(): graceful shutdown для scheduler ---
    import atexit
    atexit.register(lambda: scheduler.shutdown())

if __name__ == '__main__':
    main()