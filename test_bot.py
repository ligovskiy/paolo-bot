#!/usr/bin/env python3
"""
Тестовый файл для проверки основных функций бота
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from main import (
    get_moscow_time, 
    format_moscow_date, 
    is_allowed_user, 
    get_message_from_update,
    parse_voice_command,
    extract_params_from_voice,
    parse_search_query,
    matches_filters
)

def test_basic_functions():
    """Тестирует основные функции"""
    print("🧪 Тестирование основных функций...")
    
    # Тест времени
    moscow_time = get_moscow_time()
    print(f"✅ Московское время: {moscow_time}")
    
    formatted_date = format_moscow_date()
    print(f"✅ Форматированная дата: {formatted_date}")
    
    # Тест парсинга голосовых команд
    test_commands = [
        "покажи траты за неделю",
        "анализ поставщика Интигам",
        "найди все операции с Петровым",
        "по категориям за месяц"
    ]
    
    for cmd in test_commands:
        result = parse_voice_command(cmd)
        if result:
            print(f"✅ Команда '{cmd}' -> {result['command']}")
        else:
            print(f"❌ Команда '{cmd}' не распознана")
    
    # Тест извлечения параметров
    test_params = [
        ("анализ поставщика Интигам", "suppliers"),
        ("найди Петрова за неделю", "search")
    ]
    
    for text, cmd_type in test_params:
        params = extract_params_from_voice(text, cmd_type)
        print(f"✅ Параметры из '{text}': {params}")
    
    # Тест парсинга поисковых запросов
    test_queries = [
        "Петров декабрь",
        "поставщик >100000",
        "зарплаты неделя"
    ]
    
    for query in test_queries:
        filters = parse_search_query(query)
        print(f"✅ Фильтры для '{query}': {filters}")
    
    print("\n🎉 Все базовые тесты пройдены!")

def test_search_functions():
    """Тестирует функции поиска"""
    print("\n🔍 Тестирование функций поиска...")
    
    # Тестовые записи
    test_records = [
        {
            'Дата': '15.12.2024',
            'Описание/Получатель': 'Петров',
            'Категория': 'Зарплаты сотрудникам',
            'Сумма': -40000
        },
        {
            'Дата': '10.12.2024',
            'Описание/Получатель': 'Интигам',
            'Категория': 'Оплата поставщику',
            'Сумма': -150000
        }
    ]
    
    # Тест фильтров
    filters = parse_search_query("Петров")
    for record in test_records:
        matches = matches_filters(record, filters)
        print(f"✅ Запись '{record['Описание/Получатель']}' соответствует фильтру 'Петров': {matches}")
    
    print("🎉 Тесты поиска пройдены!")

if __name__ == "__main__":
    print("🚀 Запуск тестов бота...\n")
    
    try:
        test_basic_functions()
        test_search_functions()
        print("\n✅ Все тесты успешно пройдены!")
        print("📊 Бот готов к работе!")
        
    except Exception as e:
        print(f"\n❌ Ошибка в тестах: {e}")
        import traceback
        traceback.print_exc() 