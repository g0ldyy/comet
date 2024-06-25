import os, uvicorn, dotenv

dotenv.load_dotenv()

if __name__ == "__main__":
    uvicorn.run(
        "comet.main:app",
        host=os.getenv("FASTAPI_HOST", "127.0.0.1"),
        port=int(os.getenv("FASTAPI_PORT", "8000")),
        workers=int(os.getenv("FASTAPI_WORKERS", 2*(os.cpu_count() or 1))),
    )