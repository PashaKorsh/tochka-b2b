# NeoMarket B2B - Seller Cabinet

Реализация модуля B2B (кабинет продавца) для проекта NeoMarket.

## Структура проекта

```
backend/
├── core/           # Общие компоненты (auth, config)
├── modules/
│   ├── auth/       # Аутентификация продавцов
│   ├── categories/ # Категории товаров
│   └── products/   # Управление товарами (US-B2B-01)
├── database.py     # Настройка БД
└── main.py         # FastAPI приложение

tests/
└── test_us_b2b_01.py  # Тесты для US-B2B-01
```

## Установка и запуск

### Требования
- Python 3.11+
- PostgreSQL 14+

### Установка зависимостей

```bash
pip install -r requirements.txt
```

### Настройка базы данных

1. Создайте базу данных PostgreSQL:
```sql
CREATE DATABASE tochkab2b;
CREATE DATABASE tochkab2b_test;
```

2. Настройте переменные окружения:
```bash
export DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/tochkab2b"
export SECRET_KEY="your-secret-key-change-in-production"
```

### Запуск приложения

```bash
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

API будет доступен по адресу: http://localhost:8000

Документация API (Swagger): http://localhost:8000/docs

### Запуск тестов

```bash
pytest tests/ -v
```

## US-B2B-01: Создание товара

### Endpoint
```
POST /api/v1/products
```

### Реализованные сценарии (DoD)

✅ **create_product_returns_201_with_created_status**
- Товар создается со статусом CREATED
- skus=[] изначально
- deleted=False, blocked=False

✅ **seller_id_taken_from_jwt**
- seller_id берется только из JWT claims
- Защита от IDOR: seller_id из body игнорируется

✅ **missing_category_returns_400**
- Запрос без category_id возвращает 422 (Pydantic validation)

✅ **invalid_category_id_returns_400**
- Несуществующий category_id возвращает 400 с кодом INVALID_REQUEST

### Соответствие OpenAPI

Реализация полностью соответствует спецификации `b2b/openapi.yaml`:

- **Request**: `ProductCreate` (lines 1598-1628)
  - `category_id`: required UUID
  - `title`: required string
  - `description`: optional (anyOf [string, null])
  - `images`: optional, default=[]
  - `characteristics`: optional, default=[]

- **Response**: `ProductResponse` (lines 1740-1800)
  - Все обязательные поля из openapi
  - Дополнительно `deleted` и `blocked` из канона

### Ключевые отличия от других команд (исправленные ошибки)

1. **description** - опциональное (не required)
   - Другие команды: `min_length=1` (strengthen request)
   - Наша реализация: `Optional[str]` согласно openapi

2. **images** - опциональное, default=[]
   - Другие команды: `min_length=1` (strengthen request)
   - Наша реализация: `default_factory=list` согласно openapi

3. **category_id** в response - плоский UUID
   - Другие команды: nested object `{id, name, level, path}`
   - Наша реализация: плоский `category_id` согласно openapi

4. **deleted и blocked** - добавлены в response
   - Другие команды: отсутствуют
   - Наша реализация: включены согласно канону b2b-flows.md

## ADR: Хранение характеристик товара

### Контекст
Необходимо выбрать способ хранения характеристик товара (бренд, страна-производитель и т.д.).

### Рассмотренные варианты

1. **JSON-поле в таблице Product**
   - Плюсы: простота, не нужны JOIN
   - Минусы: сложная фильтрация, нет индексов, нет валидации на уровне БД

2. **Отдельная таблица ProductCharacteristic (выбрано)**
   - Плюсы: легко добавлять новые характеристики, простые запросы с фильтрацией
   - Минусы: требуется JOIN при выборке товара

3. **EAV-схема (Entity-Attribute-Value)**
   - Плюсы: гибкость, типизация значений
   - Минусы: сложные запросы, много JOIN, избыточная нормализация

### Решение
Выбран вариант 2: отдельная таблица `ProductCharacteristic`.

### Критерии выбора
1. **Простота запросов при фильтрации**: можно легко фильтровать товары по характеристикам через WHERE
2. **Удобство добавления новых характеристик**: просто INSERT новой записи, не нужно менять схему

### Обоснование
- Для B2B-модуля важна возможность фильтрации товаров по характеристикам (например, "все товары бренда Apple")
- Количество характеристик на товар небольшое (2-5), поэтому JOIN не критичен для производительности
- EAV избыточна для нашего случая, т.к. все значения - строки

## Технологический стек

- **FastAPI** - веб-фреймворк
- **SQLAlchemy 2.0** - ORM с async поддержкой
- **Pydantic 2.x** - валидация данных
- **PostgreSQL** - база данных
- **pytest + httpx** - тестирование
- **Alembic** - миграции БД

## Лицензия

MIT
