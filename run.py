import uvicorn
import os

if __name__ == "__main__":
    # Render ya AWS par PORT automatically set hota hai, local ke liye 8000 default rakha hai
    port = int(os.environ.get("PORT", 8000))
    
    # "app.main:app" ka matlab hai app folder ke andar main.py me jo 'app' variable hai, use chalao.
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True)
