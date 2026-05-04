import asyncio
import json
import os
import tempfile

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

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


@app.post("/run-scraper")
async def run_scraper(request: Request):
    body = await request.json()
    profile = body.get("profile")

    if not profile:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="No profile provided")

    tmp = tempfile.NamedTemporaryFile(
        mode='w', suffix='.json', delete=False
    )
    json.dump(profile, tmp)
    tmp.close()
    profile_path = tmp.name

    async def stream():
        try:
            process = await asyncio.create_subprocess_exec(
                "python3", "scraper.py",
                "--profile", profile_path,
                "--verbose",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            async for line in process.stdout:
                text = line.decode().strip()
                if text:
                    yield f"data: {json.dumps({'type': 'progress', 'message': text})}\n\n"

            await process.wait()

            result = {"type": "done", "roles": 0}

            if os.path.exists("open_roles.csv"):
                with open("open_roles.csv", encoding="utf-8") as f:
                    csv_data = f.read()
                result["open_roles_csv"] = csv_data
                result["roles"] = max(0, csv_data.count("\n") - 1)

            if os.path.exists("last_run_summary.json"):
                with open("last_run_summary.json", encoding="utf-8") as f:
                    result["last_run_summary"] = json.load(f)

            yield f"data: {json.dumps(result)}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            try:
                os.unlink(profile_path)
            except OSError:
                pass

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
