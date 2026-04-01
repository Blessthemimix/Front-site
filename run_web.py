"""Run FastAPI app process."""

import asyncio
import uvicorn
from bot_app.main_web import build_web_app
# Не вызываем настройки тут вручную, build_web_app сам внутри должен их вызвать, 
# либо передаем их туда:

async def initialize():
    return await build_web_app()

app = asyncio.run(initialize())

def main():
    uvicorn.run(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    main()