from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings

# Initialize FastAPI App
app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json"
)

# CORS Middleware (Taki aapka frontend bina kisi error ke is backend se data le sake)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Production me isko apni website ke domain se replace karenge
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {
        "platform": settings.PROJECT_NAME, 
        "status": "Online",
        "vision": "To be the ultimate Algo Trading SaaS"
    }
