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

### ✅ Соответствие merged spec (b2b/neomarket-b2b.yaml)

Реализация приведена к объединённой спецификации `b2b/neomarket-b2b.yaml`
(репозиторий `neomarket-protocols`) — путь (а) арбитража.

**Request: ProductCreate**
- `category_id`: UUID, required
- `title`: string, required (1-255 символов)
- `description`: string, **required** (1-5000 символов) — канон и spec требуют обязательное описание
- `images`: array, **required, минимум 1 элемент** — бизнес-инвариант канона: без фото карточка не попадает в каталог B2C
- `characteristics`: array, optional, default=[]

**Response: ProductResponse**
- Все обязательные поля spec, включая `slug`, `blocking_reason_id` (nullable), `moderator_comment` (nullable)
- `skus` — полный `SKUResponse` (seller-view с `cost_price`, `reserved_quantity`, `active_quantity`)
- Дополнительно `blocked` (требуется каноном; spec-клиенты игнорируют лишние поля)
- `category_id` возвращается плоским UUID

**Error: единый формат `{"code": ..., "message": ..., "details"?: ...}`**
- 400 `INVALID_REQUEST` — несуществующая категория
- 401 `UNAUTHORIZED` — нет/невалидный токен
- 403 `FORBIDDEN`, 404 `NOT_FOUND` — через глобальный handler
- 422 `VALIDATION_ERROR` — ошибка валидации тела запроса
- Глобальные exception-handler'ы в `backend/main.py` переопределяют дефолтный
  FastAPI-формат `{"detail": ...}` для всех 4xx

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
- `test_product_without_images_is_rejected` — без images → 422 (минимум 1 обязателен)
- `test_product_without_description_is_rejected` — без description → 422 (обязательное)
- `test_unauthorized_request_returns_401` — 401 с телом `{"code": "UNAUTHORIZED", ...}`
- `test_response_contains_all_required_fields` — полнота response, включая `slug`,
  `blocking_reason_id`, `moderator_comment`

## Приведение к merged spec (арбитраж, путь «а»)

### 1. ProductResponse: добавлены поля spec
- `slug` (string, required) — генерируется из `title` при создании
  (`ProductService._generate_slug`), всегда непустой
- `blocking_reason_id` (uuid, nullable) — `None` до блокировки модерацией
- `moderator_comment` (string, nullable) — `None` до ревью модератором

### 2. skus: полный SKUResponse вместо урезанного
`skus` отдаёт seller-view `SKUResponse` с `cost_price`, `reserved_quantity`,
`active_quantity` (= stock − reserved, computed-property на модели `SKU`).

### 3. Единый формат ошибок
Глобальные exception-handler'ы в `backend/main.py`:
- `RequestValidationError` → 422 `{"code": "VALIDATION_ERROR", "message", "details"}`
- `HTTPException` → `{"code", "message"}` с маппингом статуса в код (401/403/404/409/…)

### 4. description — обязательное поле
`description` обязательное, 1-5000 символов — согласно канону (b2b-flows.md)
и `b2b/neomarket-b2b.yaml#ProductCreate` (`required: [title, description, category_id]`).
В `ProductResponse` `description` — non-nullable string, поэтому request-поле
не может быть опциональным.

### 5. images — обязательное, минимум 1
`images` обязателен минимум с 1 элементом. Это сознательное ужесточение
относительно spec (`default: []`) в пользу бизнес-инварианта канона
(«Minimum 1 image required»): без фото товар не появляется в каталоге B2C.

### 6. category_id в response — плоский UUID
Плоский `category_id: UUID` согласно spec ProductResponse.

### 7. deleted и blocked в response
`deleted` — обязательное поле spec. `blocked` — дополнительно из канона
(b2b-flows.md); spec-совместимые клиенты игнорируют лишние поля.

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
