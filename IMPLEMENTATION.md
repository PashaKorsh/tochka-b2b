# US-B2B-01: Создание товара - Реализация

## Описание

Реализован endpoint `POST /api/v1/products` для создания карточки товара продавцом.

## Что реализовано

### ✅ Соответствие канону (b2b-flows.md#create-product)

1. **Товар создается со статусом CREATED**
   - `status = ProductStatus.CREATED` по умолчанию
   - `skus = []` изначально (пустой массив)
   - `deleted = False`, `blocked = False`

2. **seller_id берется из JWT claims**
   - Защита от IDOR: `seller_id` извлекается только из токена
   - Попытка передать `seller_id` в body игнорируется
   - Реализовано через `get_current_seller()` dependency

3. **Товар НЕ отправляется на модерацию**
   - Для модерации нужен хотя бы один SKU (US-B2B-02)
   - Нет побочных эффектов при создании

### ✅ Соответствие OpenAPI (b2b/openapi.yaml)

**Request: ProductCreate (lines 1598-1628)**
- `category_id`: UUID, required
- `title`: string, required (1-255 символов)
- `description`: string | null, optional
- `images`: array, optional, default=[]
- `characteristics`: array, optional, default=[]

**Response: ProductResponse (lines 1740-1800)**
- Все обязательные поля из openapi
- Дополнительно `deleted` и `blocked` (требуются каноном)
- `category_id` возвращается плоским UUID (не nested object)

**Error: ErrorResponse**
- 400: `{"code": "INVALID_REQUEST", "message": "..."}`
- 401: `{"code": "UNAUTHORIZED", "message": "..."}`
- 422: Pydantic validation error

### ✅ Тесты (DoD)

Все тесты из канона реализованы и проходят:

1. ✅ `test_create_product_returns_201_with_created_status`
   - Товар создается с status=CREATED
   - skus=[], deleted=False, blocked=False

2. ✅ `test_seller_id_taken_from_jwt`
   - seller_id из JWT, не из body
   - IDOR prevention

3. ✅ `test_missing_category_returns_400`
   - Отсутствие category_id → 422

4. ✅ `test_invalid_category_id_returns_400`
   - Несуществующий category_id → 400 INVALID_REQUEST

**Дополнительные тесты:**
- `test_product_without_images_is_allowed` - images опциональны
- `test_product_with_optional_description` - description опциональное
- `test_unauthorized_request_returns_401` - проверка авторизации
- `test_response_contains_all_required_fields` - полнота response

## Ключевые исправления ошибок других команд

### 1. description - опциональное поле
**Проблема других команд:** `min_length=1` (strengthen request)
**Наше решение:** `Optional[str]` согласно openapi.yaml:1608-1611 (anyOf [string, null])

### 2. images - опциональное поле с default=[]
**Проблема других команд:** `min_length=1` (strengthen request)
**Наше решение:** `default_factory=list` согласно openapi.yaml:1612-1617

### 3. category_id в response - плоский UUID
**Проблема других команд:** nested object `{id, name, level, path}` (breaking change)
**Наше решение:** плоский `category_id: UUID` согласно openapi.yaml:1750-1752

### 4. deleted и blocked в response
**Проблема других команд:** отсутствуют
**Наше решение:** добавлены согласно канону b2b-flows.md:89-91

## ADR: Хранение характеристик товара

### Контекст
Необходимо выбрать способ хранения характеристик товара (бренд, страна-производитель и т.д.).

### Рассмотренные варианты

#### 1. JSON-поле в таблице Product
**Плюсы:**
- Простота реализации
- Не нужны JOIN при выборке

**Минусы:**
- Сложная фильтрация по характеристикам
- Нет индексов на значения внутри JSON
- Нет валидации на уровне БД

#### 2. Отдельная таблица ProductCharacteristic ✅ (выбрано)
**Плюсы:**
- Легко добавлять новые характеристики (просто INSERT)
- Простые запросы с фильтрацией (WHERE на таблице)
- Можно создать индексы для быстрого поиска

**Минусы:**
- Требуется JOIN при выборке товара (некритично для 2-5 характеристик)

#### 3. EAV-схема (Entity-Attribute-Value)
**Плюсы:**
- Максимальная гибкость
- Типизация значений (string, int, bool)

**Минусы:**
- Сложные запросы с множественными JOIN
- Избыточная нормализация для простых случаев
- Overhead на 2-3 дополнительные таблицы

### Решение
Выбран **вариант 2: отдельная таблица ProductCharacteristic**.

### Критерии выбора

1. **Простота запросов при фильтрации**
   - Можно легко фильтровать товары: `WHERE characteristics.name = 'Бренд' AND characteristics.value = 'Apple'`
   - Для JSON потребовались бы сложные jsonb-операторы

2. **Удобство добавления новых характеристик**
   - Просто INSERT новой записи
   - Не нужно менять схему БД или миграции

### Обоснование
- Для B2B-модуля важна фильтрация товаров по характеристикам (например, "все товары бренда Apple")
- Количество характеристик на товар небольшое (2-5), поэтому JOIN не критичен
- EAV избыточна, т.к. все значения - строки (нет необходимости в типизации)

## Структура проекта

```
backend/
├── core/
│   ├── __init__.py
│   └── auth.py              # JWT authentication, get_current_seller
├── modules/
│   ├── auth/
│   │   ├── __init__.py
│   │   └── models.py        # Seller model
│   ├── categories/
│   │   ├── __init__.py
│   │   └── models.py        # Category model
│   └── products/
│       ├── __init__.py
│       ├── models.py        # Product, ProductImage, ProductCharacteristic, SKU
│       ├── schemas.py       # Pydantic schemas (ProductCreate, ProductResponse)
│       ├── service.py       # Business logic (ProductService)
│       └── router.py        # FastAPI endpoints
├── database.py              # SQLAlchemy setup
└── main.py                  # FastAPI app

tests/
├── __init__.py
└── test_us_b2b_01.py        # Tests for US-B2B-01
```

## Технологический стек

- **FastAPI 0.136.1** - веб-фреймворк
- **SQLAlchemy 2.0.49** - ORM с async поддержкой
- **Pydantic 2.13.3** - валидация данных
- **PostgreSQL 15** - база данных
- **pytest 8.0.0 + httpx** - тестирование
- **python-jose** - JWT токены

## Запуск

### Docker Compose (рекомендуется)
```bash
docker-compose up -d
```

API доступен на http://localhost:8000
Swagger UI: http://localhost:8000/docs

### Локально
```bash
# Установка зависимостей
pip install -r requirements.txt

# Настройка .env
cp .env.example .env

# Запуск
uvicorn backend.main:app --reload
```

### Тесты
```bash
# Создать тестовую БД
createdb tochkab2b_test

# Запустить тесты
pytest tests/ -v
```

## Проверка DoD

### ✅ Соответствие user-flow
- [x] Товар создается со статусом CREATED
- [x] seller_id из JWT
- [x] Валидация обязательных полей
- [x] Все тесты проходят

### ✅ Соответствие OpenAPI
- [x] Request соответствует ProductCreate
- [x] Response соответствует ProductResponse
- [x] category_id плоский (не nested)
- [x] Добавлены deleted/blocked из канона

### ✅ Тесты
- [x] create_product_returns_201_with_created_status
- [x] seller_id_taken_from_jwt
- [x] missing_category_returns_400
- [x] invalid_category_id_returns_400
- [x] Дополнительные edge cases

### ✅ ADR
- [x] Рассмотрены 3 варианта хранения характеристик
- [x] Выбран вариант с отдельной таблицей
- [x] Обоснованы критерии выбора

## Следующие шаги

1. **US-B2B-02**: Создание SKU
   - POST /api/v1/skus
   - Переход товара в ON_MODERATION при первом SKU
   - Отправка события в Moderation

2. **Миграции Alembic**
   - Создать initial migration для всех моделей

3. **CI/CD**
   - GitHub Actions для автоматического запуска тестов

## Commit
```
36525f3 feat: implement US-B2B-01 - Create Product endpoint
```
