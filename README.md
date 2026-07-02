# Custom Songs Converter Service

HTTP API для мода Custom Songs. Сервис принимает ссылку, скачивает аудио через `yt-dlp`, конвертирует в `.ogg` через `ffmpeg` и возвращает файл моду.

## Render

Создай **Web Service** с Docker runtime.

Обязательная переменная окружения:

```text
CUSTOMSONGS_CONVERTER_KEY=любой_длинный_секретный_ключ
```

Endpoint для мода:

```text
https://<render-service-name>.onrender.com/convert
```

## Local

```bash
docker build -t customsongs-converter .
docker run --rm -p 8000:8000 -e CUSTOMSONGS_CONVERTER_KEY=dev-key customsongs-converter
```

Health check:

```bash
curl http://localhost:8000/health
```
