import os, uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "comet.main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        # workers=int(os.getenv("WORKERS", 2*(os.cpu_count() or 1))), # Disabled for development
        # ssl_keyfile="./key.pem", # For development
        # ssl_certfile="./cert.pem" # For development
    )
