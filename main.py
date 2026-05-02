from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="RoleScout API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.michaophir.com",
                   "https://getrolescout.com",
                   "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok", "service": "rolescout-scraper"}
