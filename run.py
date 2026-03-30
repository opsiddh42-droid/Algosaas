import uvicorn

if __name__ == "__main__":
    # "app.main:app" ka matlab hai app folder ke andar main.py me jo 'app' variable hai, use chalao.
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
