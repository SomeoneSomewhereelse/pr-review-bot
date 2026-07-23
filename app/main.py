from fastapi import FastAPI, Request, Response

app = FastAPI(title="pr-review-engine")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request):
    await request.body()
    return Response(status_code=202)
